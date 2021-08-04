# NiklasRosenstein/conda-feedstocks

Helper to manage my conda-forge feedstocks.

__Requirements__

* Git
* GitHub Token (to automatically create forks if needed)
* Conda-Smithy (`conda install -c conda-forge conda-smithy`)

__Usage__

* List current feedstock versions compared to the expectation in `feedstocks.yml`:

    ```sh
    $ python feedstocks.py --list
    ```

* Fork `conda-forge/staged-recipes` and create a new branch to submit a new recipe via Grayskull:

    ```sh
    $ python feedstocks.py --create <package_name> --token <github_token>
    ```

* Fork `conda-forge/<package>-feedstock` and create a new branch with an updated recipe and repo files;

    ```sh
    $ python feedstocks.py --update <package_name> --token <github_token>
    ```

The pull requests still need to be created manually afterwards.
