package fs

import "os"

func CopyFile(src string, dest string) error {
	bytes, err := os.ReadFile(src)
	if err != nil {
		return err
	}
	err = os.Remove(dest)
	if err != nil {
		return err
	}
	err = os.WriteFile(dest, bytes, 0777)
	if err != nil {
		return err
	}
	return nil
}
