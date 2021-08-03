
import argparse
import re
import dataclasses
import os
import shutil
import subprocess
import sys
import tempfile
import typing as t

import databind.json
import nr.utils.git
import requests
import yaml
from grayskull import __version__ as grayskull_version
from grayskull.cli import CLIConfig
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


def generate_recipe(output_dir: str, package: str, version: str) -> None:
  CLIConfig().stdout = True
  CLIConfig().list_missing_deps = True
  recipe = GrayskullFactory.create_recipe("pypi", package, version)
  recipe.generate_recipe(output_dir)


def generate_recipe_into(output_dir: str, package: str, version: str) -> None:
  with tempfile.TemporaryDirectory() as tempdir:
    generate_recipe(tempdir, package, version)
    shutil.copytree(os.path.join(tempdir, package), output_dir, dirs_exist_ok=True)


def ensure_repo_is_cloned(
  repo: nr.utils.git.Git,
  clone_url: str,
  upstream_url: t.Optional[str] = None,
  after_clone_steps: t.Optional[str] = None,
) -> None:
  if os.path.isdir(repo.path):
    return
  print(colored(f'Cloning {os.path.basename(repo.path)}/staged-recipes...', 'green'))
  repo.clone(clone_url)
  if upstream_url:
    repo.add_remote('upstream', upstream_url)
  if after_clone_steps:
    repo.check_call(['bash', '-c', after_clone_steps])


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
  clone_url = f'git@github.com:{config.github_user}/staged-recipes'
  upstream_url = f'https://github.com/conda-forge/staged-recipes.git'
  ensure_repo_is_cloned(repo, clone_url, upstream_url, config.after_clone)
  repo.fetch('upstream')

  print(colored(f'Creating {package} {version}', 'green'))
  repo.create_branch(f'add-{package}', reset=True, ref='upstream/master')
  repo.reset('upstream/master', hard=True)

  output_dir = os.path.join(repo.path, 'recipes')
  generate_recipe(output_dir, package, version)

  repo.add([os.path.relpath(output_dir, repo.path)])
  repo.commit(f"Generated recipe for {package}@{version} with grayskull {grayskull_version}.")
  cb = repo.get_current_branch_name()
  repo.push('origin', f'{cb}:{cb}', force=True)


def _do_update(config: Config, package: str, version: str) -> None:
  repo = nr.utils.git.Git(f'data/{package}-feedstock')
  clone_url = f'git@github.com:{config.github_user}/{package}-feedstock'
  upstream_url = f'https://github.com/conda-forge/{package}-feedstock'
  ensure_repo_is_cloned(repo, clone_url, upstream_url, config.after_clone)

  print(f'Creating upgrade PR for {package}@{version}')
  repo.create_branch(f'upgrade-to-{version}', reset=True, ref='upstream/master')
  repo.reset('upstream/master', hard=True)

  output_dir = os.path.join(repo.path, 'recipe')
  generate_recipe_into(output_dir, package, version)

  repo.add([os.path.relpath(output_dir, repo.path)])
  repo.commit(f"{package}@{version} (grayskull {grayskull_version})")
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
    _do_update(config, args.update, package_versions[args.update])
    return

  parser.print_usage()


if __name__ == '__main__':
  main()
