
import argparse
import re
import dataclasses
import os
import subprocess
import sys
import typing as t

import databind.json
import grayskull
from grayskull.cli import CLIConfig
import nr.utils.git
import requests
import yaml
from grayskull import __version__ as grayskull_version
from grayskull.base.factory import GrayskullFactory
from termcolor import colored

RECIPE_RAW_URL_TEMPLATE = 'https://raw.githubusercontent.com/conda-forge/{package}-feedstock/master/recipe/meta.yaml'
STAGED_RECIPES_CLONE_URL_TEMPLATE = 'https://github.com/{user}/staged-recipes.git'


@dataclasses.dataclass
class Config:
  github_user: str
  feedstocks: t.List[str]
  after_clone: t.Optional[str] = None


def get_feedstock_meta_yaml(package_name: str) -> t.Optional[str]:
  url = RECIPE_RAW_URL_TEMPLATE.format(package=package_name)
  response = requests.get(url)
  if response.status_code == 404:
    return None
  response.raise_for_status()
  return response.text


def get_version_from_meta_yaml(meta_yaml: str) -> str:
  return re.search(r'{%\s*set\s+version\s*=\s*"(.*?)"\s*%}', meta_yaml).group(1)


def get_argument_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser()
  parser.add_argument('-l', '--list', action='store_true', help='List the latest status of each feedstock.')
  parser.add_argument('-c', '--create', metavar='feedstock', help='Create a staged feedstock.')
  parser.add_argument('-u', '--update', metavar='feedstock', help='Update the recipe for a feedstock.')
  parser.add_argument('--user', help='Name of your GitHub user. The staged-recipes repository must already be forked.')
  return parser


def _do_list(package_versions: t.Dict[str, str]) -> None:
  for package, target_version in package_versions.items():
    meta_yaml = get_feedstock_meta_yaml(package)
    if meta_yaml:
      latest_version = get_version_from_meta_yaml(meta_yaml)
      if latest_version == target_version:
        print(colored(f'{package} {target_version}', 'green'))
      else:
        print(colored(f'{package} {latest_version} (expected {target_version})', 'yellow'))
    else:
      print(colored(f'{package} not found', 'red'))


def _do_create(config: Config, package: str, version: str) -> None:
  meta_yaml = get_feedstock_meta_yaml(package)
  if meta_yaml:
    print(colored(f'error: the feedstock for {package} already exists, try using -u,--update', 'red'))
    sys.exit(1)

  repo = nr.utils.git.Git('data/staged-recipes')

  if not os.path.isdir(repo.path):
    print(colored(f'Cloning {config.github_user}/staged-recipes...', 'green'))
    clone_url = f'git@github.com:{config.github_user}/staged-recipes'
    upstream_url = f'https://github.com/conda-forge/staged-recipes.git'
    try:
      repo.clone(clone_url)
    except subprocess.CalledProcessError:
      print(colored('error: You may need to fork conda-forge/staged-recipes first.', 'red'))
      sys.exit(1)
    repo.add_remote('upstream', upstream_url)
    if config.after_clone:
      repo.check_call(['bash', '-c', config.after_clone])
  repo.fetch('upstream')

  print(colored(f'Creating {package} {version}', 'green'))
  repo.create_branch(f'add-{package}', reset=True, ref='upstream/master')
  repo.reset('upstream/master', hard=True)

  CLIConfig().stdout = True
  CLIConfig().list_missing_deps = True
  recipe = GrayskullFactory.create_recipe(
    "pypi",
    package,
    version)

  output_dir = os.path.join(repo.path, 'recipes')
  recipe.generate_recipe(output_dir)

  repo.add([os.path.relpath(output_dir, repo.path)])
  repo.commit(f"Generated recipe for {package}@{version} with grayskull {grayskull_version}.")
  cb = repo.get_current_branch_name()
  repo.push('origin', f'{cb}:{cb}', force=True)


def main():
  parser = get_argument_parser()
  args = parser.parse_args()

  with open('feedstocks.yml') as fp:
    config = databind.json.load(yaml.safe_load(fp), Config)
  package_versions = dict(package.split('@') for package in config.feedstocks)

  if args.list:
    _do_list(package_versions)
    return

  if args.create:
    _do_create(config, args.create, package_versions[args.create])
    return

  if args.update:
    return

  parser.print_usage()


if __name__ == '__main__':
  main()
