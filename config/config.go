package config

import "path"

const HOME = "/Users/devonfulcher"
const DEFAULT_BRANCH = "main"
const REPO_NAME = "toolbelt"
const EXECUTABLE_NAME = "toolbelt"
const SLG_REPO = "semantic-layer-gateway"
const RUNTIME_GATEWAY_REPO = "runtime-gateway"
const DOTFILES_REPO = "dotfiles"
const GITHUB_USERNAME = "DevonFulcher"
const DEVSPACE_NAMESPACE = "dev-devonfulcher"

var REPOS_PATH = path.Join(HOME, "git")
var CLI_PATH = path.Join(HOME, "cli")
var SLG_PATH = path.Join(REPOS_PATH, SLG_REPO)
var RUNTIME_GATEWAY_PATH = path.Join(REPOS_PATH, RUNTIME_GATEWAY_REPO)
var DOTFILES_PATH = path.Join(REPOS_PATH, DOTFILES_REPO)

// https://code.visualstudio.com/docs/getstarted/settings#_settings-file-locations
var VSCODE_DOTFILES_SETTINGS = path.Join(DOTFILES_PATH, "shared/vscode/settings.json")
var VSCODE_USER_SETTINGS = path.Join(HOME, "Library/Application Support/Code/User/settings.json")

var VSCODE_DOTFILES_EXTENSIONS = path.Join(DOTFILES_PATH, "shared/vscode/extensions.txt")
