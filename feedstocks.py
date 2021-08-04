
import argparse
import enum
import dataclasses
import os
import re
import shutil
import subprocess
import sys
import tempfile
import typing as t

import databind.json
import github
from grayskull.base.base_recipe import AbstractRecipeModel
import nr.utils.git
import requests
import termcolor
import yaml
from grayskull import __version__ as grayskull_version
from grayskull.cli import CLIConfig
from grayskull.base.factory import GrayskullFactory
from termcolor import cprint

RECIPE_RAW_URL_TEMPLATE = 'https://raw.githubusercontent.com/conda-forge/{package}-feedstock/master/recipe/meta.yaml'
STAGED_RECIPES_CLONE_URL_TEMPLATE = 'https://github.com/{user}/staged-recipes.git'


@dataclasses.dataclass
class Config:
  github_user: str
  feedstocks: t.List[str]
  conda_bin: t.Optional[str] = None
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


def generate_recipe(
  output_dir: str,
  package: str,
  version: str,
  process_recipe: t.Optional[t.Callable[[AbstractRecipeModel], t.Any]] = None,
) -> None:
  CLIConfig().stdout = True
  CLIConfig().list_missing_deps = True
  recipe = GrayskullFactory.create_recipe("pypi", package, version)
  if process_recipe:
    process_recipe(recipe)
  recipe.generate_recipe(output_dir)
  CLIConfig().stdout = False
  CLIConfig().list_missing_deps = False
  print(termcolor.RESET, end='')


def generate_recipe_into(
  output_dir: str,
  package: str,
  version: str,
  process_recipe: t.Optional[t.Callable[[AbstractRecipeModel], t.Any]] = None,
) -> None:
  with tempfile.TemporaryDirectory() as tempdir:
    generate_recipe(tempdir, package, version, process_recipe)
    # TODO (NiklasRosenstein): Might need to read recipe name after processing.
    shutil.copytree(os.path.join(tempdir, package), output_dir, dirs_exist_ok=True)


def get_argument_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser()
  parser.add_argument('-t', '--token', help='The GitHub token.')
  parser.add_argument('-b', '--branch', help='The branch name. If not specified, a default branch name will be selected.')
  parser.add_argument('-l', '--list', action='store_true', help='List the latest status of each feedstock.')
  parser.add_argument('-c', '--create', metavar='feedstock', nargs='*', help='Create (a) staged recipe(s). Without arguments, all missing recipes will be created.')
  parser.add_argument('-u', '--update', metavar='feedstock', help='Update the recipe for a feedstock.')
  parser.add_argument('-g', '--generate', help='Generate the recipe for all unpublished repositories into the specified directory.')
  parser.add_argument('-p', '--prefix', help='Prefix recipes with the specified name.')
  return parser


@dataclasses.dataclass
class Options:

  class Action(enum.Enum):
    list = enum.auto()
    create = enum.auto()
    update = enum.auto()
    generate = enum.auto()

  token: t.Optional[str] = None
  branch: t.Optional[str] = None
  action: t.Optional[Action] = None
  create_feedstocks: t.Optional[t.List[str]] = None
  update_feedstock: t.Optional[str] = None
  generate_dir: t.Optional[str] = None

  @classmethod
  def from_args(cls, args: argparse.Namespace) -> 'Options':
    self = cls()
    self.token = args.token
    self.branch = args.branch
    self.create_feedstocks = args.create
    self.update_feedstock = args.update
    self.generate_dir = args.generate

    if sum(1 for _ in (args.list, args.create is not None, args.update) if _) > 1:
      raise ValueError('multiple operations specified')
    if args.list:
      self.action = Options.Action.list
    elif args.create is not None:
      self.action = Options.Action.create
    elif args.update:
      self.action = Options.Action.update
    elif args.generate:
      self.action = Options.Action.generate

    return self

  def get_github_client(self) -> t.Optional[github.Github]:
    if self.token:
      return github.Github(self.token)
    return None


class FeedstocksManager:

  def __init__(self, config: Config, gh: t.Optional[github.Github], prefix: t.Optional[str] = None) -> None:
    self._config = config
    self._gh = gh
    self._prefix = prefix

  def _get_package_versions(self) -> t.Dict[str, str]:
    return dict(package.split('@') for package in self._config.feedstocks)

  def _ensure_repo_is_cloned(
    self,
    repo: nr.utils.git.Git,
    clone_url: str,
    upstream_url: t.Optional[str] = None,
    after_clone_steps: t.Optional[str] = None,
  ) -> None:
    if os.path.isdir(repo.path):
      return
    repo.clone(clone_url)
    if upstream_url:
      repo.add_remote('upstream', upstream_url)
    if after_clone_steps:
      repo.check_call(['bash', '-c', after_clone_steps])

  def _ensure_fork_exists(self, original_owner: str, repo: str) -> None:
    assert self._gh is not None
    user_name = self._gh.get_user().login
    assert user_name == self._config.github_user, 'github user mismatch in config'
    try:
      self._gh.get_repo(user_name  + '/' + repo)
    except github.UnknownObjectException:
      cprint(f'Forking {original_owner}/{repo}...', 'cyan')
      self._gh.get_repo(original_owner + '/' + repo).create_fork()
      # TODO (NiklasRosenstein): Wait for fork to be completed?

  def get_unpublished_packages(self) -> t.List[str]:
    return [p for p in self._get_package_versions() if not get_feedstock_meta_yaml(p)]

  def list_feedstock_status(self) -> None:
    package_versions = self._get_package_versions()
    for package, target_version in package_versions.items():
      meta_yaml = get_feedstock_meta_yaml(package)
      if meta_yaml:
        latest_version = get_version_from_meta_yaml(meta_yaml)
        if latest_version == target_version:
          cprint(f'{package} {target_version}', 'green')
        else:
          cprint(f'{package} {latest_version} (expected {target_version})', 'yellow')
      else:
        cprint(f'{package} not found', 'red')

  def create_feedstocks(self, packages: t.List[str], branch_name: t.Optional[str] = None) -> None:
    package_versions = self._get_package_versions()
    package_versions = {p: package_versions[p] for p in packages}

    for package in package_versions:
      meta_yaml = get_feedstock_meta_yaml(package)
      if meta_yaml:
        cprint(f'error: the feedstock for {package} already exists, try using -u,--update', 'red')
        sys.exit(1)

    repo = nr.utils.git.Git('data/staged-recipes')
    clone_url = f'git@github.com:{self._config.github_user}/staged-recipes'
    upstream_url = f'https://github.com/conda-forge/staged-recipes.git'
    if self._gh:
      self._ensure_fork_exists('conda-forge', 'staged-recipes')
    self._ensure_repo_is_cloned(repo, clone_url, upstream_url, self._config.after_clone)
    repo.fetch('upstream')

    if not branch_name:
      branch_name = 'add-' + '-'.join(package_versions)
      if len(branch_name) > 30:
        branch_name = f'add-{len(package_versions)}-packages'
    repo.create_branch(branch_name, reset=True, ref='upstream/master')
    repo.reset('upstream/master', hard=True)

    output_dir = os.path.join(repo.path, 'recipes')
    for package, version in package_versions.items():
      cprint(f'Creating {package} {version}', 'green')
      generate_recipe(output_dir, package, version, self._process_recipe)

    repo.add([os.path.relpath(output_dir, repo.path)])
    repo.commit(f'Add {", ".join(package_versions)}')
    cb = repo.get_current_branch_name()
    repo.push('origin', f'{cb}:{cb}', force=True)

  def update_feedstock(self, package: str, branch_name: t.Optional[str] = None) -> None:
    version = self._get_package_versions()[package]
    repo = nr.utils.git.Git(f'data/{package}-feedstock')
    clone_url = f'git@github.com:{self._config.github_user}/{package}-feedstock'
    upstream_url = f'https://github.com/conda-forge/{package}-feedstock'
    if self._gh:
      self._ensure_fork_exists('conda-forge', f'{package}-feedstock')
    self._ensure_repo_is_cloned(repo, clone_url, upstream_url, self._config.after_clone)
    repo.fetch('upstream')

    print(f'Creating upgrade PR for {package}@{version}')
    branch_name = branch_name or f'upgrade-to-{version}'
    repo.create_branch(branch_name, reset=True, ref='upstream/master')
    repo.reset('upstream/master', hard=True)

    output_dir = os.path.join(repo.path, 'recipe')
    generate_recipe_into(output_dir, package, version, self._process_recipe)

    conda_bin = os.path.expanduser(self._config.conda_bin) if self._config.conda_bin else 'conda'
    subprocess.check_call([conda_bin, 'smithy', 'rerender'], cwd=repo.path)

    repo.add([os.path.relpath(output_dir, repo.path)])
    repo.commit(f"{package}@{version} (grayskull {grayskull_version})")
    cb = repo.get_current_branch_name()
    repo.push('origin', f'{cb}:{cb}', force=True)

  def generate_recipes(self, recipes_dir: str) -> None:
    package_versions = self._get_package_versions()
    os.makedirs(recipes_dir, exist_ok=True)
    for package in self.get_unpublished_packages():
      generate_recipe(recipes_dir, package, package_versions[package], self._process_recipe)

  def _process_recipe(self, recipe: AbstractRecipeModel) -> None:
    if not self._prefix:
      return

    # Add the prefix to the recipe name and to requirements known to the feedstock manager.
    packages = set(self._get_package_versions())
    name = recipe['package']['name'].values[0]
    recipe.set_var_content(name, self._prefix + recipe.get_var_content(name))
    for section in recipe['requirements']:
      for idx, item in enumerate(section):
        package_name = re.split(r'[\s<>=!]', item.value)[0]
        if package_name in packages:
          item.value = self._prefix + item.value


def main():
  parser = get_argument_parser()
  args = parser.parse_args()

  with open('feedstocks.yml') as fp:
    config = databind.json.load(yaml.safe_load(fp), Config)

  options = Options.from_args(args)
  if not options.action:
    parser.print_usage()
    return

  manager = FeedstocksManager(config, options.get_github_client(), args.prefix)
  if options.action == Options.Action.list:
    manager.list_feedstock_status()
  elif options.action == Options.Action.create:
    assert options.create_feedstocks is not None
    if not options.create_feedstocks:
      options.create_feedstocks = manager.get_unpublished_packages()
    cprint(f'Creating staged recipe for {len(options.create_feedstocks)} packages.: '
      f'{", ".join(options.create_feedstocks)}', 'cyan')
    manager.create_feedstocks(options.create_feedstocks, options.branch)
  elif options.action == Options.Action.update:
    assert options.update_feedstock
    manager.update_feedstock(options.update_feedstock, options.branch)
  elif options.action == Options.Action.generate:
    assert options.generate_dir
    manager.generate_recipes(options.generate_dir)
  else:
    parser.error('something unexpected happened')


if __name__ == '__main__':
  main()
