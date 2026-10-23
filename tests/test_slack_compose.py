"""The Slack messages for a PR, as the pr-publish skill writes them."""

from toolbelt.ui.slack_compose import (
    BumpPr,
    Person,
    PostPr,
    compose_bump,
    compose_post,
    compose_reassign,
    short_context,
    to_slack,
)

ALAN = Person("alan.andradecestelos", "U07TJ969BFT")
DANIEL = Person("daniel.baptista dias", "U082T1XSR2Q")


def pr(number: int, title: str, assignee=ALAN, parent=None, lines=(10, 2)) -> PostPr:
    return PostPr(
        number=number,
        url=f"https://github.com/o/r/pull/{number}",
        title=title,
        additions=lines[0],
        deletions=lines[1],
        assignee=assignee,
        parent_number=parent,
    )


def test_the_context_is_the_title_without_its_ticket_key_and_capitalised():
    assert short_context("DI-5561: serve a generic detail") == "Serve a generic detail"
    assert short_context("RD-1301938 Merge AsyncLLMClient") == "Merge AsyncLLMClient"
    assert short_context("No key here") == "No key here"
    assert short_context("") == ""


def test_a_single_pr_post():
    text = compose_post([pr(7, "DI-1: Add alerting on main", lines=(381, 9))])

    assert text == (
        "- [Add alerting on main](https://github.com/o/r/pull/7) @alan.andradecestelos `+381/-9`\n"
        "\n"
        "Thanks for your time!"
    )


def test_a_stack_is_indented_under_the_pr_it_is_stacked_on_bottom_first():
    text = compose_post(
        [
            pr(1, "Bottom", lines=(5, 1)),
            pr(2, "Middle", parent=1),
            pr(3, "Top", parent=2),
            pr(9, "Separate"),
        ]
    )

    assert text.splitlines()[0:4] == [
        "- [Bottom](https://github.com/o/r/pull/1) @alan.andradecestelos `+5/-1`",
        "  - [Middle](https://github.com/o/r/pull/2) @alan.andradecestelos `+10/-2`",
        "    - [Top](https://github.com/o/r/pull/3) @alan.andradecestelos `+10/-2`",
        "- [Separate](https://github.com/o/r/pull/9) @alan.andradecestelos `+10/-2`",
    ]


def test_a_pr_stacked_on_one_not_in_the_post_is_a_top_level_bullet():
    text = compose_post([pr(2, "Child", parent=1)])

    assert text.splitlines()[0].startswith("- [Child]")


def test_a_pr_without_an_assignee_has_no_mention():
    text = compose_post([pr(7, "Add it", assignee=None)])

    assert "pull/7) `+10/-2`" in text


def bump_pr(number: int, repo="dbt-labs/ai-codegen-api", lines=(96, 6)) -> BumpPr:
    return BumpPr(
        repo=repo,
        number=number,
        url=f"https://github.com/{repo}/pull/{number}",
        additions=lines[0],
        deletions=lines[1],
    )


def test_a_bump_names_the_pr_as_a_link_with_its_size_and_asks_the_different_assignee():
    assert compose_bump([bump_pr(1795)], DANIEL) == (
        "It has been over 24 hours and "
        "[ai-codegen-api#1795](https://github.com/dbt-labs/ai-codegen-api/pull/1795) `+96/-6` "
        "has no comments or reviews yet. "
        "@daniel.baptista dias, would you mind taking a look? Thanks!"
    )


def test_a_bump_for_several_prs_lists_them_with_their_sizes():
    text = compose_bump(
        [
            bump_pr(1, lines=(1, 1)),
            bump_pr(2, repo="o/other", lines=(2, 2)),
            bump_pr(3),
        ],
        ALAN,
    )

    assert (
        "[ai-codegen-api#1](https://github.com/dbt-labs/ai-codegen-api/pull/1) `+1/-1`, "
        "[other#2](https://github.com/o/other/pull/2) `+2/-2` and "
        "[ai-codegen-api#3](https://github.com/dbt-labs/ai-codegen-api/pull/3) `+96/-6` "
        "have no comments or reviews yet"
    ) in text


def test_a_bump_without_a_person_asks_anyone():
    assert compose_bump([bump_pr(1)], None).endswith(
        "`+96/-6` has no comments or reviews yet. Would someone mind taking a look? Thanks!"
    )


def test_names_become_slack_mentions_and_longer_names_win():
    text = "@alan.andradecestelos, and @daniel.baptista dias. Also @alan.andradecestelos2 stays."
    mentions = {"alan.andradecestelos": "U1", "daniel.baptista dias": "U2"}

    got = to_slack(text, mentions)

    assert "<@U1>, and <@U2>. Also @alan.andradecestelos2 stays." in got


def test_no_line_about_who_wrote_the_message_is_added():
    assert to_slack("hello", {}) == "hello"
    assert "AI-generated" not in compose_post([pr(7, "Add it")])
    assert "AI-generated" not in compose_bump([bump_pr(1)], ALAN)


def test_a_reassignment_asks_whether_the_pr_may_be_reassigned_to_the_person():
    assert compose_reassign(bump_pr(1795), DANIEL) == (
        "@daniel.baptista dias, would you mind if I reassign "
        "[ai-codegen-api#1795](https://github.com/dbt-labs/ai-codegen-api/pull/1795) "
        "to you? Thanks!"
    )


def test_a_reassignment_with_nobody_chosen_asks_anyone():
    assert compose_reassign(bump_pr(1795), None) == (
        "Would someone mind taking a look at "
        "[ai-codegen-api#1795](https://github.com/dbt-labs/ai-codegen-api/pull/1795)? Thanks!"
    )
