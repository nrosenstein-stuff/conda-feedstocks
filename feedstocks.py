
import argparse
import enum
import dataclasses
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import typing as t
from pathlib import Path

import databind.json
import github
import jinja2
import networkx
import nr.utils.git
import requests
import termcolor
import yaml
from grayskull import __version__ as grayskull_version
from grayskull.base.base_recipe import AbstractRecipeModel
from grayskull.cli import CLIConfig
from grayskull.base.factory import GrayskullFactory
from grayskull.pypi import PyPi
from termcolor import cprint

RECIPE_RAW_URL_TEMPLATE = 'https://raw.githubusercontent.com/conda-forge/{package}-feedstock/master/recipe/meta.yaml'
STAGED_RECIPES_CLONE_URL_TEMPLATE = 'https://github.com/{user}/staged-recipes.git'


@dataclasses.dataclass
class Config:
  github_user: str
  feedstocks: t.List[str]
  conda_bin: t.Optional[str] = None
  after_clone: t.Optional[str] = None

  def get_conda_bin(self) -> str:
    return os.path.expanduser(self.conda_bin) if self.conda_bin else 'conda'


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

  # Capture the package name if it is changed in *process_recipe*.
  def _processor(recipe: AbstractRecipeModel) -> None:
    nonlocal package
    if process_recipe:
      process_recipe(recipe)
      package = recipe.get_var_content(recipe['package']['name'].values[0])

  with tempfile.TemporaryDirectory() as tempdir:
    generate_recipe(tempdir, package, version, _processor)
    shutil.copytree(os.path.join(tempdir, package), output_dir, dirs_exist_ok=True)


def get_argument_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser()
  parser.add_argument('packages', nargs='*', help='A list of packages for the current action.')
  parser.add_argument('-t', '--token', help='The GitHub token.')
  parser.add_argument('-b', '--branch', help='The branch name. If not specified, a default branch name will be selected.')
  parser.add_argument('-l', '--list', action='store_true', help='List the latest status of each feedstock.')
  parser.add_argument('-c', '--create', action='store_true', help='Create staged recipes.')
  parser.add_argument('-u', '--update', action='store_true', help='Update the recipe for a feedstock.')
  parser.add_argument('--prefix', help='Add a prefix to generated recipes and requirements known to the feedstock manager.')
  parser.add_argument('--generate', help='Generate the recipe for the specified packages (or all unpublished packages).')
  # TODO (NiklasRosenstein): Option(s) to include/exclude packages not for generating but for prefixing in requirements.
  parser.add_argument('--build', default=NotImplemented, nargs='?', help='Build recipes from the specified directory (or the same directory as --generate).')
  parser.add_argument('--build-channel', action='append', help='Add conda channels for building.')
  parser.add_argument('--no-test', action='store_true', help='Dont test packages after build.')
  parser.add_argument('--publish', default=NotImplemented, nargs='?', help='Publish built packages from the specified directory (or the same directory as --generate/--build)')
  parser.add_argument('--to', help='Publish built packages to the specified repository URL.')
  return parser


@dataclasses.dataclass
class Options:

  class Action(enum.Enum):
    LIST = enum.auto()
    CREATE = enum.auto()
    UPDATE = enum.auto()
    BUILD_AND_STUFF = enum.auto()

  token: t.Optional[str] = None
  branch: t.Optional[str] = None
  action: t.Optional[Action] = None
  packages: t.List[str] = dataclasses.field(default_factory=list)
  prefix: t.Optional[str] = None
  build_channels: t.List[str] = dataclasses.field(default_factory=list)
  generate_dir: t.Optional[str] = None
  build_from_dir: t.Optional[str] = None
  publish_from_dir: t.Optional[str] = None
  publish_to: t.Optional[str] = None
  no_test: bool = False

  @classmethod
  def from_args(cls, args: argparse.Namespace) -> 'Options':
    self = cls()
    self.token = args.token
    self.branch = args.branch
    self.packages = args.packages
    self.build = args.build
    self.build_channels = args.build_channel or []
    self.no_test = args.no_test

    if sum(1 for _ in (args.list, args.create, args.update, args.generate) if _) > 1:
      raise ValueError('multiple operations specified')
    if args.list:
      self.action = Options.Action.LIST
    elif args.create:
      self.action = Options.Action.CREATE
    elif args.update:
      self.action = Options.Action.UPDATE
    elif args.generate or args.build or args.publish:
      self.generate_dir = args.generate
      self.build_from_dir = None if args.build is NotImplemented else (args.build or self.generate_dir)
      self.publish_from_dir = None if args.publish is NotImplemented else (args.publish or self.build_from_dir)
      self.publish_to = args.to
      self.action = Options.Action.BUILD_AND_STUFF

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

    subprocess.check_call([self._config.get_conda_bin(), 'smithy', 'rerender'], cwd=repo.path)

    repo.add([os.path.relpath(output_dir, repo.path)])
    repo.commit(f"{package}@{version} (grayskull {grayskull_version})")
    cb = repo.get_current_branch_name()
    repo.push('origin', f'{cb}:{cb}', force=True)

  def generate_recipes(self, recipes_dir: str, packages: t.List[str]) -> None:
    package_versions = self._get_package_versions()
    os.makedirs(recipes_dir, exist_ok=True)
    for package in packages:
      parent_dir = os.path.join(recipes_dir, (self._prefix or '') + package)
      generate_recipe_into(parent_dir, package, package_versions[package], lambda r: self._process_recipe(r, packages))

  def _process_recipe(self, recipe: AbstractRecipeModel, known_packages: t.Optional[t.Sequence[str]] = None) -> None:
    if not self._prefix:
      return
    packages = set(known_packages if known_packages is not None else self._get_package_versions())

    # Add the prefix to the recipe name and to requirements known to the feedstock manager.
    # NOTE: We could set the "name" variable, but it would impact also the PyPI source URL.
    name = recipe['package']['name'].values[0]
    name.value = self._prefix + recipe.get_var_content(name)
    for section in recipe['requirements']:
      for item in section:
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

  if options.action == Options.Action.LIST:
    manager.list_feedstock_status()

  elif options.action == Options.Action.CREATE:
    assert options.packages is not None
    if not options.packages:
      options.packages = manager.get_unpublished_packages()
    cprint(f'Creating staged recipes for {len(options.packages)} packages: f{", ".join(options.packages)}')
    manager.create_feedstocks(options.packages, options.branch)

  elif options.action == Options.Action.UPDATE:
    assert len(options.packages) == 1, len(options.packages)
    manager.update_feedstock(options.packages[0], options.branch)

  elif options.action == Options.Action.BUILD_AND_STUFF:
    if options.generate_dir:
      cprint(f'Generating recipes into {options.generate_dir}', 'magenta')
      if not options.packages:
        options.packages = manager.get_unpublished_packages()
      cprint(f'  {len(options.packages)} packages: f{", ".join(options.packages)}')
      manager.generate_recipes(options.generate_dir, options.packages)

    if options.build_from_dir:
      cprint(f'Building recipes in {options.build_from_dir}', 'magenta')
      build_dir = os.path.join(options.build_from_dir, 'build')

      packages: t.Dict[str, t.Dict[str, t.Any]] = {}
      for directory in os.listdir(options.build_from_dir):
        if directory == 'build': continue
        with open(os.path.join(options.build_from_dir, directory, 'meta.yaml')) as fp:
          out = jinja2.Environment().from_string(fp.read()).render()
          packages[directory] = yaml.safe_load(out)

      # Sort packages topologically.
      graph = networkx.DiGraph()
      graph.add_nodes_from(packages)
      for package, meta in packages.items():
        for section in meta['requirements']:
          for dep in meta['requirements'][section]:
            package_name = re.split(r'[\s<>=!]', dep)[0]
            if package_name in packages:
              graph.add_edge(package_name, package)

      add_args = []
      for channel in options.build_channels:
        add_args += ['-c', channel]
      if options.no_test:
        add_args += ['--no-test']
      for package in networkx.algorithms.topological_sort(graph):
        recipe_dir = os.path.join(options.build_from_dir, package)
        subprocess.check_call([config.get_conda_bin(), 'build', recipe_dir, '--output-folder', build_dir] + add_args)

    if options.publish_from_dir:
      cprint(f'Publishing built packages from {options.publish_from_dir}/build', 'magenta')
      build_dir = Path(options.publish_from_dir) / 'build'
      for channel in build_dir.iterdir():
        if not channel.is_dir(): continue
        for file_ in channel.iterdir():
          if not file_.name.endswith('.tar.bz2'): continue
          cprint(f'> {file_}', 'cyan')
          with file_.open('rb') as fp:
            url = posixpath.join(options.publish_to, channel.name, file_.name)
            requests.put(url, data=fp).raise_for_status()

  else:
    parser.error('something unexpected happened')


if __name__ == '__main__':
  main()
