# README.MD - acestor v0.1.0

This package contains various tools and module to build and deploy disease models. The current version focuses on dengue models.

## Usage

To use this package in your environment to run disease models - it's recommended you use [poetry](̌https://python-poetry.org/). You can install poetry using the steps listed in their official documentation. It's also recommended you use a conda environment as a wrapper, but you are free to use other virtual environments as per your preference. The instructions below assume usage of ```poetry``` and ```conda```.

```acestor``` requires ```python>=3.9```, and is not yet available on the ```PyPI``` registry. You can install and use it so:

```bash
cd my-project
conda create -n my-project-dev python=3.9 -y
conda activate my-project-dev
poetry init # follow the steps suggested
# Install from git using https
poetry add git+https://github.com/dsih-artpark/acestor.git
# OR install using SSH, which might require additional authentication.
poetry add git+ssh://git@github.com:dsih-artpark/acestor.git
poetry lock # lock epipipeline as a project dependency
```

It is not recommended that you use pip to install this package, but if you prefer to do that, use:
```bash
pip install git+https://github.com/dsih-artpark/acestor.git
```

After this, you can use ```acestor``` as a regular package in your environment. 

## Contributing

To contribute, if you have access to create branches, you can clone and create a pull request with your changes. Else, you can do the same from a [fork](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/working-with-forks/fork-a-repo).

```bash
git clone https://github.com/dsih-artpark/acestor.git

git checkout -b sk-glm-generalise
# sk stands for your initials, e.g. mine stand for Sneha Kanmani. 
# Be sure to mention what the feature or patch is about.

# Make your changes to the code and save them

# Add and commit your changes.
git add acestor.file.changes.made.to
git commit -m "feat: added functionality to optionally use other types of generalised linear models"
git push -u origin sk-gis-feat
```

For commit messages, we recommend using [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0-beta.4/), which is inspired by the Angular Convention on Commits.

After that, create a pull request on github.com or using the ```github cli``` (official documentation [here](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/proposing-changes-to-your-work-with-pull-requests/creating-a-pull-request?tool=cli)). 