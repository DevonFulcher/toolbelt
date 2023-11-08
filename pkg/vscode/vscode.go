package vscode

import (
	"errors"
	"os"
	"strings"
	"toolbelt/internal/config"
	"toolbelt/pkg/comparable"
	"toolbelt/pkg/shell"
)

func PullExtensions() error {
	c := shell.New("code --list-extensions")
	out, err := c.RunCmd()
	if err != nil {
		return err
	}
	prior := strings.Split(out, "\n")

	bytes, err := os.ReadFile(config.VSCODE_DOTFILES_EXTENSIONS)
	if err != nil {
		return err
	}
	remote := strings.Split(string(bytes), "\n")

	installationErrs := []string{}
	toInstall := comparable.Subtract(remote, prior)
	for _, ext := range toInstall {
		c = shell.New("code --install-extension %v", ext)
		_, err = c.RunCmd()
		if err != nil {
			installationErrs = append(installationErrs, err.Error())
		}
	}

	toUninstall := comparable.Subtract(prior, remote)
	for _, ext := range toUninstall {
		c = shell.New("code --uninstall-extension %v", ext)
		c.RunCmd()
		if err != nil {
			installationErrs = append(installationErrs, err.Error())
		}
	}

	return errors.New(strings.Join(installationErrs, "\n"))
}
