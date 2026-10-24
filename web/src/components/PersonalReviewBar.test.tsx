import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { FileReviewInfo, PersonalReviewState } from "../api/generated";
import { PersonalReviewBar } from "./ReviewView";

afterEach(() => {
  vi.unstubAllGlobals();
});

const file = (path: string, over: Partial<FileReviewInfo> = {}): FileReviewInfo => ({
  path,
  old_path: null,
  status: "M",
  diff_hash: `hash-${path}`,
  state: "unreviewed",
  note: "",
  ...over,
});

const state = (over: Partial<PersonalReviewState> = {}): PersonalReviewState => ({
  id: "1:devon/a",
  files: [file("a.py"), file("b.py", { state: "approved" })],
  result: "pass",
  summary: "Fine, one nit.",
  opening_pr: false,
  notice: null,
  can_post_in_slack: true,
  ...over,
});

/** A fetch recording what is sent; ``reply`` is what the server answers. */
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
  test("shows what the validate run found and how many files are approved", () => {
    render(<PersonalReviewBar state={state()} selected="a.py" onDone={() => {}} />);

    expect(screen.getByText(/1 of 2 files approved/)).toBeInTheDocument();
    expect(screen.getByText(/the validate run would approve: Fine, one nit\./)).toBeInTheDocument();
  });

  test("approving a file sends that file's path and the hash of its diff, and no Slack post by default", async () => {
    const sent = serve();
    const done = vi.fn();
    render(<PersonalReviewBar state={state()} selected="a.py" onDone={done} />);

    fireEvent.click(screen.getByRole("button", { name: "Approve a.py" }));

    expect(screen.getByRole("button", { name: "Approving…" })).toBeDisabled();
    await waitFor(() => expect(done).toHaveBeenCalled());
    expect(sent).toEqual([
      {
        path: "/api/review/file",
        body: { id: "1:devon/a", path: "a.py", diff_hash: "hash-a.py", state: "approved", note: "", post_in_slack: false },
      },
    ]);
  });

  test("requesting changes to a file needs a note", async () => {
    const sent = serve();
    render(<PersonalReviewBar state={state()} selected="a.py" onDone={() => {}} />);
    const request = screen.getByRole("button", { name: "Request changes" });
    expect(request).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Note"), { target: { value: "rename it" } });
    fireEvent.click(request);

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].body).toMatchObject({ path: "a.py", state: "changes_requested", note: "rename it" });
  });

  test("approve all sends every file with its hash, and the Slack option when ticked", async () => {
    const sent = serve();
    render(<PersonalReviewBar state={state()} selected={null} onDone={() => {}} />);

    fireEvent.click(screen.getByLabelText(/Post in Slack/));
    fireEvent.click(screen.getByRole("button", { name: "Approve all" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toEqual({
      path: "/api/review/approve-all",
      body: { id: "1:devon/a", files: { "a.py": "hash-a.py", "b.py": "hash-b.py" }, note: "", post_in_slack: true },
    });
  });

  test("the Slack option is offered only when Slack is configured", () => {
    render(<PersonalReviewBar state={state({ can_post_in_slack: false })} selected={null} onDone={() => {}} />);

    expect(screen.queryByLabelText(/Post in Slack/)).toBeNull();
  });

  test("a file with a decision says so, and without a file picked there is nothing to approve singly", () => {
    const { rerender } = render(
      <PersonalReviewBar
        state={state({ files: [file("a.py", { state: "changes_requested", note: "rename it" })] })}
        selected="a.py"
        onDone={() => {}}
      />,
    );
    expect(screen.getByText("a.py: changes requested: rename it")).toBeInTheDocument();

    rerender(<PersonalReviewBar state={state()} selected={null} onDone={() => {}} />);
    expect(screen.getByRole("button", { name: "Pick a file to approve" })).toBeDisabled();
  });

  test("a refusal is shown, and the buttons come back to try again", async () => {
    serve({ ok: false, status: 409, detail: "a.py changed since this page loaded: reload it." });
    render(<PersonalReviewBar state={state()} selected="a.py" onDone={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: "Approve a.py" }));

    expect(await screen.findByText(/changed since this page loaded/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve a.py" })).toBeEnabled();
  });

  test("while the PR is opening there is nothing to press, and a failure is shown", () => {
    const { rerender } = render(<PersonalReviewBar state={state({ opening_pr: true })} selected="a.py" onDone={() => {}} />);
    expect(screen.getByText(/opening its PR/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve all" })).toBeNull();

    rerender(<PersonalReviewBar state={state({ notice: "Opening the PR failed: boom" })} selected="a.py" onDone={() => {}} />);
    expect(screen.getByText("Opening the PR failed: boom")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve all" })).toBeEnabled();
  });
});
