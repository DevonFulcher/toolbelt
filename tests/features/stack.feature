Feature: Stack sync
  A stack is a chain of branches, each recorded as stacked on its parent.
  Syncing merges each parent's new work into its children, and once a
  parent's PR has landed, restacks its children onto the next surviving
  ancestor and cleans up the landed branch.

  Scenario: Sync propagates a parent's new commit down the stack
    Given branch "devon/api" stacked on "main"
    And branch "devon/api_tests" stacked on "devon/api"
    When "api.txt" is committed on "devon/api"
    And the stack is synced from "devon/api_tests"
    Then "devon/api_tests" contains "api.txt"

  Scenario: Restack after a squash-merge land
    Given branch "devon/api" stacked on "main"
    And branch "devon/api_tests" stacked on "devon/api"
    And "api.txt" is committed on "devon/api"
    And "tests.txt" is committed on "devon/api_tests"
    When "devon/api" is squash-merged into "main"
    And the stack is synced from "devon/api_tests"
    Then "devon/api_tests" is stacked on "main"
    And "devon/api_tests" contains "api.txt"
    And "devon/api_tests" contains "tests.txt"
    And branch "devon/api" no longer exists
