package comparable

func Subtract[T comparable](left []T, right []T) []T {
	result := []T{}
	rightMap := make(map[T]bool)
	for _, item := range right {
		rightMap[item] = true
	}

	for _, item := range left {
		if !rightMap[item] {
			result = append(result, item)
		}
	}
	return result
}
