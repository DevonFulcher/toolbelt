import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { SlackAccepted, SlackDraft, SlackDraftPr } from "../api/generated";
import { SlackDialog } from "./SlackDialog";

afterEach(() => {
  vi.unstubAllGlobals();
});

const draftPr = (number: number, title: string, over: Partial<SlackDraftPr> = {}): SlackDraftPr => ({
  repo: "o/r",
  number,
  url: `https://github.com/o/r/pull/${number}`,
  title,
  additions: 20,
  deletions: 4,
  assignee: { id: "UA", name: "alan" },
  included: true,
  pr: "open",
  ci: "success",
  review: "requested",
  open_comments: 0,
  has_conflicts: false,
  next: { label: "Post in Slack", who: "you", kind: "post_slack", would_be: null },
  ...over,
});

const draft = (over: Partial<SlackDraft> = {}): SlackDraft => ({
  kind: "post",
  channel: "DTEST",
  text: "- [Bottom](https://github.com/o/r/pull/1) @alan `+20/-4`\n\nThanks for your time!",
  assignee: { id: "UA", name: "alan" },
  candidates: [
    { id: "UW", name: "will" },
    { id: "UA", name: "alan" },
  ],
  prs: [draftPr(1, "Bottom")],
  thread_ts: null,
  thread_link: null,
  ...over,
});

const two = (): SlackDraft =>
  draft({
    text: [
      "- [Bottom](https://github.com/o/r/pull/1) @alan `+20/-4`",
      "  - [Top](https://github.com/o/r/pull/12) @alan `+5/-1`",
      "",
      "Thanks for your time!",
    ].join("\n"),
    prs: [draftPr(1, "Bottom"), draftPr(12, "Top", { additions: 5, deletions: 1 })],
  });

const accepted = (): SlackAccepted => ({ prs: [{ repo: "o/r", number: 1 }] });

/** A fetch that answers the draft and send endpoints; records what it was sent. */
function serve(opts: { draft?: SlackDraft | { status: number; detail: string }; send?: SlackAccepted | { status: number; detail: string } }) {
  const calls: { path: string; body: unknown }[] = [];
  const answer = (value: unknown) => {
    const failure = value as { status?: number; detail?: string };
    if (failure.status !== undefined && failure.detail !== undefined) {
      return { ok: false, status: failure.status, json: async () => ({ detail: failure.detail }) };
    }
    return { ok: true, status: 200, json: async () => value };
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init: RequestInit) => {
      const body = JSON.parse(String(init.body)) as unknown;
      calls.push({ path, body });
      return path === "/api/slack/draft" ? answer(opts.draft ?? draft()) : answer(opts.send ?? accepted());
    }),
  );
  return calls;
}

const open = async (onClose: () => void = () => undefined) => {
  render(<SlackDialog id="1:a" onClose={onClose} />);
  return screen.findByRole("textbox");
};

describe("the Post in Slack dialog", () => {
  test("shows the message, where it goes and who is asked, and sends nothing yet", async () => {
    const calls = serve({});

    const box = await open();

    expect(box).toHaveValue(draft().text);
    expect(screen.getByRole("dialog", { name: "Post in Slack" })).toBeInTheDocument();
    expect(screen.getByText("DTEST")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "o/r#1" })).toHaveAttribute("href", "https://github.com/o/r/pull/1");
    expect(screen.getByRole("combobox")).toHaveValue("UA");
    expect(calls).toEqual([{ path: "/api/slack/draft", body: { id: "1:a", assignee_id: null } }]);
  });

  test("asking someone else renames them in the text and keeps what was edited", async () => {
    serve({});
    const box = await open();
    fireEvent.change(box, { target: { value: `${draft().text}\nPlease look today @alan.` } });

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "UW" } });

    expect(box).toHaveValue(draft().text.replace("@alan", "@will") + "\nPlease look today @will.");
  });

  test("Post sends the edited text, who was asked and the PRs, then closes: it posts in the background", async () => {
    const calls = serve({});
    const onClose = vi.fn();
    const box = await open(onClose);
    fireEvent.change(box, { target: { value: "edited https://github.com/o/r/pull/1 @alan" } });

    fireEvent.click(screen.getByRole("button", { name: "Post" }));

    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    expect(calls[1]).toEqual({
      path: "/api/slack/send",
      body: {
        id: "1:a",
        kind: "post",
        text: "edited https://github.com/o/r/pull/1 @alan",
        assignee_id: "UA",
        thread_ts: null,
        prs: [{ repo: "o/r", number: 1, assignee_id: "UA" }],
      },
    });
  });

  test("while the server takes the message the button says it is sending", async () => {
    let release: () => void = () => undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (path: string) => {
        if (path === "/api/slack/draft") return { ok: true, status: 200, json: async () => draft() };
        await new Promise<void>((resolve) => (release = resolve));
        return { ok: true, status: 202, json: async () => accepted() };
      }),
    );
    await open();

    fireEvent.click(screen.getByRole("button", { name: "Post" }));

    expect(await screen.findByRole("button", { name: "Sending…" })).toBeDisabled();
    release();
  });

  test("a failed send says why and can be tried again", async () => {
    serve({ send: { status: 502, detail: "the Slack session timed out" } });
    await open();

    fireEvent.click(screen.getByRole("button", { name: "Post" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("the Slack session timed out");
    expect(screen.getByRole("button", { name: "Post" })).toBeEnabled();
  });

  test("an empty message cannot be sent", async () => {
    serve({});
    const box = await open();

    fireEvent.change(box, { target: { value: "  " } });

    expect(screen.getByRole("button", { name: "Post" })).toBeDisabled();
  });

  test("when there is nothing to post the dialog says why", async () => {
    serve({ draft: { status: 409, detail: "Nothing to post or bump: the branch's next action is not about Slack." } });
    render(<SlackDialog id="1:a" onClose={() => undefined} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Nothing to post or bump");
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  describe("a post of several PRs", () => {
    test("each PR has its own picker, and the one above them asks everyone", async () => {
      serve({ draft: two() });
      await open();

      expect(screen.getByLabelText("Ask about o/r#1")).toHaveValue("UA");
      expect(screen.getByLabelText("Ask about o/r#12")).toHaveValue("UA");
      expect(screen.getByLabelText("Ask everyone")).toHaveValue("UA");
    });

    test("a single PR has just the one picker", async () => {
      serve({});
      await open();

      expect(screen.queryByLabelText("Ask about o/r#1")).toBeNull();
      expect(screen.getByLabelText("Ask")).toBeInTheDocument();
    });

    test("asking someone else about one PR changes only its line", async () => {
      serve({ draft: two() });
      const box = await open();

      fireEvent.change(screen.getByLabelText("Ask about o/r#12"), { target: { value: "UW" } });

      const lines = (box as HTMLTextAreaElement).value.split("\n");
      expect(lines[0]).toContain("@alan");
      expect(lines[1]).toContain("@will");
      expect(screen.getByLabelText("Ask everyone")).toHaveValue(""); // different people now
    });

    test("asking everyone changes every line and every picker", async () => {
      serve({ draft: two() });
      const box = await open();
      fireEvent.change(screen.getByLabelText("Ask about o/r#12"), { target: { value: "UW" } });

      fireEvent.change(screen.getByLabelText("Ask everyone"), { target: { value: "UW" } });

      expect((box as HTMLTextAreaElement).value).not.toContain("@alan");
      expect(screen.getByLabelText("Ask about o/r#1")).toHaveValue("UW");
      expect(screen.getByLabelText("Ask about o/r#12")).toHaveValue("UW");
    });

    test("sends each PR with the person asked about it", async () => {
      const calls = serve({ draft: two(), send: { prs: [] } });
      await open();
      fireEvent.change(screen.getByLabelText("Ask about o/r#12"), { target: { value: "UW" } });

      fireEvent.click(screen.getByRole("button", { name: "Post" }));

      await waitFor(() => expect(calls).toHaveLength(2));
      expect((calls[1].body as { prs: unknown }).prs).toEqual([
        { repo: "o/r", number: 1, assignee_id: "UA" },
        { repo: "o/r", number: 12, assignee_id: "UW" },
      ]);
    });
  });

  describe("which PRs the message covers", () => {
    const held = (): SlackDraft => {
      const base = draft({
        text: "- [Top](https://github.com/o/r/pull/12) @alan `+5/-1`\n\nThanks for your time!",
        prs: [
          draftPr(1, "Bottom", {
            included: false,
            ci: "failed",
            open_comments: 2,
            has_conflicts: true,
            next: {
              label: "With an agent",
              who: "agent",
              kind: "claimed",
              would_be: { label: "Post in Slack", who: "you", kind: "post_slack" },
            },
          }),
          draftPr(12, "Top", { additions: 5, deletions: 1 }),
        ],
      });
      return base;
    };

    test("a PR an agent holds starts unchecked and says why, beside what else is known of each PR", async () => {
      serve({ draft: held() });
      await open();

      expect(screen.getByRole("checkbox", { name: "Include o/r#1" })).not.toBeChecked();
      expect(screen.getByRole("checkbox", { name: "Include o/r#12" })).toBeChecked();
      const list = screen.getByRole("list", { name: "PRs in the message" });
      expect(within(list).getByText(/CI failed/)).toHaveTextContent(
        "+20/-4 · CI failed · review requested · 2 open comments · merge conflicts · With an agent (would be: Post in Slack)",
      );
      expect(within(list).getByText(/\+5\/-1/)).toHaveTextContent("+5/-1 · CI success · review requested");
    });

    test("checking it asks the server for the message of the PRs now checked, with who is asked about each", async () => {
      const calls = serve({ draft: held() });
      await open();
      fireEvent.change(screen.getByLabelText("Ask about o/r#12"), { target: { value: "UW" } });

      fireEvent.click(screen.getByRole("checkbox", { name: "Include o/r#1" }));

      await waitFor(() => expect(calls).toHaveLength(2));
      expect(calls[1]).toEqual({
        path: "/api/slack/draft",
        body: {
          id: "1:a",
          assignee_id: null,
          prs: [
            { repo: "o/r", number: 1, assignee_id: "UA" },
            { repo: "o/r", number: 12, assignee_id: "UW" },
          ],
        },
      });
    });

    test("the message becomes what the server wrote for the PRs checked", async () => {
      const bothText = "- [Bottom](https://github.com/o/r/pull/1) @alan `+20/-4`\n  - [Top](https://github.com/o/r/pull/12) @alan `+5/-1`";
      let drafts = 0;
      vi.stubGlobal(
        "fetch",
        vi.fn(async () => ({
          ok: true,
          status: 200,
          json: async () => (drafts++ === 0 ? held() : { ...held(), text: bothText }),
        })),
      );
      const box = await open();

      fireEvent.click(screen.getByRole("checkbox", { name: "Include o/r#1" }));

      await waitFor(() => expect(box).toHaveValue(bothText));
      expect(screen.getByRole("checkbox", { name: "Include o/r#1" })).toBeChecked();
    });

    test("unchecking a PR is sent as a request for the others and Post sends only those", async () => {
      const calls = serve({ draft: two(), send: { prs: [] } });
      await open();

      fireEvent.click(screen.getByRole("checkbox", { name: "Include o/r#1" }));
      await waitFor(() => expect(calls).toHaveLength(2));
      expect((calls[1].body as { prs: unknown }).prs).toEqual([{ repo: "o/r", number: 12, assignee_id: "UA" }]);
      await waitFor(() => expect(screen.getByRole("button", { name: "Post" })).toBeEnabled());
      fireEvent.click(screen.getByRole("button", { name: "Post" }));

      await waitFor(() => expect(calls).toHaveLength(3));
      expect((calls[2].body as { prs: unknown }).prs).toEqual([{ repo: "o/r", number: 12, assignee_id: "UA" }]);
    });

    test("with nothing checked there is nothing to post", async () => {
      const calls = serve({ draft: held() });
      const box = await open();

      fireEvent.click(screen.getByRole("checkbox", { name: "Include o/r#12" }));

      await waitFor(() => expect(box).toHaveValue(""));
      expect(screen.getByRole("button", { name: "Post" })).toBeDisabled();
      expect(calls).toHaveLength(1); // no request for a message about no PR
    });

    test("a refused rewrite says why and puts the checkbox back", async () => {
      let drafts = 0;
      vi.stubGlobal(
        "fetch",
        vi.fn(async () =>
          drafts++ === 0
            ? { ok: true, status: 200, json: async () => held() }
            : { ok: false, status: 409, json: async () => ({ detail: "Pick at least one PR to post." }) },
        ),
      );
      await open();

      fireEvent.click(screen.getByRole("checkbox", { name: "Include o/r#1" }));

      expect(await screen.findByRole("alert")).toHaveTextContent("Pick at least one PR");
      expect(screen.getByRole("checkbox", { name: "Include o/r#1" })).not.toBeChecked();
    });

    test("a single PR shows what is known of it without a checkbox", async () => {
      serve({});
      await open();

      expect(screen.queryByRole("checkbox")).toBeNull();
      expect(screen.getByText(/CI success/)).toHaveTextContent("+20/-4 · CI success · review requested");
    });
  });

  test("a bump links to the original post in Slack", async () => {
    serve({
      draft: draft({
        kind: "bump",
        thread_ts: "1792500000.000100",
        thread_link: "https://s.slack.com/archives/DTEST/p1792500000000100",
        text: "It has been over 24 hours",
      }),
    });
    render(<SlackDialog id="1:a" onClose={() => undefined} />);

    const link = await screen.findByRole("link", { name: "Original post in Slack" });

    expect(link).toHaveAttribute("href", "https://s.slack.com/archives/DTEST/p1792500000000100");
  });

  test("a post has no original post to link to", async () => {
    serve({ draft: draft() });
    render(<SlackDialog id="1:a" onClose={() => undefined} />);
    await screen.findByRole("button", { name: "Post" });

    expect(screen.queryByRole("link", { name: "Original post in Slack" })).toBeNull();
  });

  test("a bump says it replies in a thread and sends that thread", async () => {
    const calls = serve({
      draft: draft({ kind: "bump", thread_ts: "1792500000.000100", text: "It has been over 24 hours" }),
    });
    render(<SlackDialog id="1:a" onClose={() => undefined} />);
    await screen.findByRole("textbox");

    expect(screen.getByRole("dialog", { name: "Bump in Slack" })).toBeInTheDocument();
    expect(screen.getByText(/Replies in a thread of/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Send bump" }));

    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].body).toMatchObject({ kind: "bump", thread_ts: "1792500000.000100" });
  });

  describe("reassigning a posted PR", () => {
    const reassignDraft = (): SlackDraft =>
      draft({
        kind: "reassign",
        thread_ts: "1792500000.000100",
        thread_link: "https://s.slack.com/archives/DTEST/p1792500000000100",
        text: "@alan, would you mind if I reassign [r#1](https://github.com/o/r/pull/1) to you? Thanks!",
      });

    test("asks the server for a reassignment and says it replies in the thread", async () => {
      const calls = serve({ draft: reassignDraft() });
      render(<SlackDialog id="1:a" reassign onClose={() => undefined} />);
      await screen.findByRole("textbox");

      expect(calls[0]).toEqual({ path: "/api/slack/draft", body: { id: "1:a", assignee_id: null, reassign: true } });
      expect(screen.getByRole("dialog", { name: "Reassign in Slack" })).toBeInTheDocument();
      expect(screen.getByText(/Replies in a thread of/)).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "Original post in Slack" })).toBeInTheDocument();
    });

    test("asking someone else renames the new reviewer in the text", async () => {
      serve({ draft: reassignDraft() });
      render(<SlackDialog id="1:a" reassign onClose={() => undefined} />);
      const box = await screen.findByRole("textbox");

      fireEvent.change(screen.getByRole("combobox"), { target: { value: "UW" } });

      expect(box).toHaveValue(reassignDraft().text.replaceAll("@alan", "@will"));
    });

    test("Send reassignment sends it as a reassignment in that thread", async () => {
      const calls = serve({ draft: reassignDraft() });
      const onClose = vi.fn();
      render(<SlackDialog id="1:a" reassign onClose={onClose} />);
      await screen.findByRole("textbox");

      fireEvent.click(screen.getByRole("button", { name: "Send reassignment" }));

      await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
      expect(calls[1].body).toMatchObject({
        kind: "reassign",
        thread_ts: "1792500000.000100",
        assignee_id: "UA",
        prs: [{ repo: "o/r", number: 1, assignee_id: "UA" }],
      });
    });
  });

  test("Close closes it", async () => {
    serve({});
    const onClose = vi.fn();
    render(<SlackDialog id="1:a" onClose={onClose} />);
    await screen.findByRole("textbox");

    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Close" }));

    expect(onClose).toHaveBeenCalled();
  });
});
