package cli

import "fmt"

type Command struct {
	Name        string
	Description string
	Children    []Command
	Run         func(params []string) error
}

func findCmd(input string, cmds []Command) (*Command, error) {
	for _, cmd := range cmds {
		if input == cmd.Name {
			return &cmd, nil
		}
	}
	return nil, fmt.Errorf("invalid input. %v is not valid", input)
}

func printDescription(cmds []Command) {
	for _, cmd := range cmds {
		line := fmt.Sprintf("%v: %v", cmd.Name, cmd.Description)
		fmt.Println(line)
	}
}

func Run(input []string, tree []Command) error {
	if len(input) == 0 {
		printDescription(tree)
		return nil
	}
	curr := tree
	var cmd *Command
	var err error
	i := 0
	for _, val := range input {
		cmd, err = findCmd(val, curr)
		i += 1
		if err != nil {
			return err
		}
		if cmd == nil || cmd.Children == nil || len(cmd.Children) == 0 {
			break
		}
		curr = cmd.Children
	}
	if cmd.Run == nil {
		printDescription(cmd.Children)
		return nil
	}
	return cmd.Run(input[i:])
}
