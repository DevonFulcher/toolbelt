package config

import (
	"os"
	"path"
)

var home = os.Getenv("HOME")

const REPO_NAME = "toolbelt"
const EXECUTABLE_NAME = "toolbelt"
const DOTFILES_REPO = "dotfiles"
const GITHUB_USERNAME = "DevonFulcher"
const DEVSPACE_NAMESPACE = "dev-devonfulcher"

var REPOS_PATH = path.Join(home, "git")
var CLI_PATH = path.Join(home, "cli")
var DOTFILES_PATH = path.Join(REPOS_PATH, DOTFILES_REPO)

var VSCODE_DOTFILES_EXTENSIONS = path.Join(DOTFILES_PATH, "vscode/extensions.txt")
