import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { PersonalReviewState } from "../api/generated";
import { PersonalReviewBar } from "./ReviewView";

afterEach(() => {
  vi.unstubAllGlobals();
});

const state = (over: Partial<PersonalReviewState> = {}): PersonalReviewState => ({
  id: "1:devon/a",
  head_sha: "abc123",
  verdict: null,
  result: "pass",
  summary: "Fine, one nit.",
  opening_pr: false,
  notice: null,
  can_post_in_slack: true,
  ...over,
});

/** A fetch recording the verdicts sent; ``reply`` is what the server answers. */
function serve(reply: { ok: boolean; status: number; detail?: string } = { ok: true, status: 202 }) {
  const sent: { path: string; body: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init?: RequestInit) => {
      sent.push({ path, body: JSON.parse(String(init?.body)) });
      return { ok: reply.ok, status: reply.status, json: async () => ({ detail: reply.detail }) };
    }),
  );
  return sent;
}

describe("the personal review bar", () => {
  test("shows what the validate run found", () => {
    render(<PersonalReviewBar state={state()} onDone={() => {}} />);

    expect(screen.getByText(/the validate run would approve: Fine, one nit\./)).toBeInTheDocument();
  });

  test("approving sends the commit that was reviewed, the note, and no Slack post by default", async () => {
    const sent = serve();
    const done = vi.fn();
    render(<PersonalReviewBar state={state()} onDone={done} />);

    fireEvent.change(screen.getByLabelText("Note"), { target: { value: "lgtm" } });
    fireEvent.click(screen.getByRole("button", { name: "Approve and open PR" }));

    expect(screen.getByRole("button", { name: "Approving…" })).toBeDisabled();
    await waitFor(() => expect(done).toHaveBeenCalled());
    expect(sent).toEqual([
      { path: "/api/review/approve", body: { id: "1:devon/a", head_sha: "abc123", note: "lgtm", post_in_slack: false } },
    ]);
  });

  test("the Slack option is offered only when Slack is configured, and is sent when ticked", async () => {
    const sent = serve();
    const { rerender } = render(<PersonalReviewBar state={state({ can_post_in_slack: false })} onDone={() => {}} />);
    expect(screen.queryByLabelText(/Post in Slack/)).toBeNull();

    rerender(<PersonalReviewBar state={state()} onDone={() => {}} />);
    fireEvent.click(screen.getByLabelText(/Post in Slack/));
    fireEvent.click(screen.getByRole("button", { name: "Approve and open PR" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].body).toMatchObject({ post_in_slack: true });
  });

  test("requesting changes needs a note", async () => {
    const sent = serve({ ok: true, status: 200 });
    render(<PersonalReviewBar state={state()} onDone={() => {}} />);
    const request = screen.getByRole("button", { name: "Request changes" });
    expect(request).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Note"), { target: { value: "rename it" } });
    fireEvent.click(request);

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toEqual({
      path: "/api/review/request-changes",
      body: { id: "1:devon/a", head_sha: "abc123", note: "rename it", post_in_slack: false },
    });
  });

  test("a refusal is shown, and the buttons come back to try again", async () => {
    serve({ ok: false, status: 409, detail: "The branch has a new commit since this page loaded: reload it." });
    render(<PersonalReviewBar state={state()} onDone={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: "Approve and open PR" }));

    expect(await screen.findByText(/new commit since this page loaded/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve and open PR" })).toBeEnabled();
  });

  test("while the PR is opening there is nothing to press, and a failure is shown", () => {
    const { rerender } = render(<PersonalReviewBar state={state({ opening_pr: true })} onDone={() => {}} />);
    expect(screen.getByText(/opening its PR/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve and open PR" })).toBeNull();

    rerender(
      <PersonalReviewBar
        state={state({
          notice: "Opening the PR failed: boom",
          verdict: { verdict: "approved", note: "", head_sha: "abc123", at: "2026-10-13T12:00:00Z" },
        })}
        onDone={() => {}}
      />,
    );
    expect(screen.getByText("Opening the PR failed: boom")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve and open PR" })).toBeEnabled();
  });
});
