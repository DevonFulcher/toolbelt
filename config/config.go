package config

import "path"

const HOME = "/Users/devonfulcher"
const DEFAULT_BRANCH = "main"
const REPO_NAME = "toolbelt"
const EXECUTABLE_NAME = "toolbelt"
const SLG_REPO = "semantic-layer-gateway"
const RUNTIME_GATEWAY_REPO = "runtime-gateway"

var REPOS_PATH = path.Join(HOME, "git")
var CLI_PATH = path.Join(HOME, "cli")
var SLG_PATH = path.Join(REPOS_PATH, SLG_REPO)
var RUNTIME_GATEWAY_PATH = path.Join(REPOS_PATH, RUNTIME_GATEWAY_REPO)
