import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { SlackDraft, SlackSent } from "../api/generated";
import { SlackDialog } from "./SlackDialog";

afterEach(() => {
  vi.unstubAllGlobals();
});

const draft = (over: Partial<SlackDraft> = {}): SlackDraft => ({
  kind: "post",
  channel: "DTEST",
  text: "🤖 AI-generated on behalf of Devon:\n- [Bottom](https://github.com/o/r/pull/1) @alan `+20/-4`\n\nThanks for your time!",
  assignee: { id: "UA", name: "alan" },
  candidates: [
    { id: "UW", name: "will" },
    { id: "UA", name: "alan" },
  ],
  prs: [{ repo: "o/r", number: 1, url: "https://github.com/o/r/pull/1", title: "Bottom", additions: 20, deletions: 4 }],
  thread_ts: null,
  ...over,
});

const sent = (over: Partial<SlackSent> = {}): SlackSent => ({
  link: "https://s.slack.com/archives/DTEST/p1792600001000100",
  verified: true,
  warning: null,
  cost_usd: 0.04,
  ...over,
});

/** A fetch that answers the draft and send endpoints; records what it was sent. */
function serve(opts: { draft?: SlackDraft | { status: number; detail: string }; send?: SlackSent | { status: number; detail: string } }) {
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
      return path === "/api/slack/draft" ? answer(opts.draft ?? draft()) : answer(opts.send ?? sent());
    }),
  );
  return calls;
}

const open = async () => {
  render(<SlackDialog id="1:a" onClose={() => undefined} />);
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

  test("Post sends the edited text, who was asked and the PRs, then says it was posted", async () => {
    const calls = serve({ send: sent({ cost_usd: 0.05 }) });
    const box = await open();
    fireEvent.change(box, { target: { value: "edited https://github.com/o/r/pull/1 @alan" } });

    fireEvent.click(screen.getByRole("button", { name: "Post" }));

    const link = await screen.findByRole("link", { name: "Posted in Slack" });
    expect(link).toHaveAttribute("href", "https://s.slack.com/archives/DTEST/p1792600001000100");
    expect(screen.getByText(/cost \$0\.05/)).toBeInTheDocument();
    expect(calls[1]).toEqual({
      path: "/api/slack/send",
      body: {
        id: "1:a",
        kind: "post",
        text: "edited https://github.com/o/r/pull/1 @alan",
        assignee_id: "UA",
        thread_ts: null,
        prs: [{ repo: "o/r", number: 1 }],
      },
    });
    expect(screen.getByRole("button", { name: "Done" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Post" })).not.toBeInTheDocument();
  });

  test("a message Slack's copy did not match is flagged", async () => {
    serve({ send: sent({ verified: false, warning: "Posted, but Slack's copy is missing: UA" }) });
    await open();

    fireEvent.click(screen.getByRole("button", { name: "Post" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Slack's copy is missing: UA");
  });

  test("a failed send says why and can be tried again", async () => {
    serve({ send: { status: 502, detail: "the Slack session timed out" } });
    await open();

    fireEvent.click(screen.getByRole("button", { name: "Post" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("the Slack session timed out");
    expect(screen.getByRole("button", { name: "Post" })).toBeEnabled();
    expect(screen.queryByRole("link", { name: "Posted in Slack" })).not.toBeInTheDocument();
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

  test("Close closes it", async () => {
    serve({});
    const onClose = vi.fn();
    render(<SlackDialog id="1:a" onClose={onClose} />);
    await screen.findByRole("textbox");

    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Close" }));

    expect(onClose).toHaveBeenCalled();
  });
});
