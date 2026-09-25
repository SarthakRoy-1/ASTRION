import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WorkspaceCreated } from "./WorkspaceCreated";

/**
 * The owner's one sight of the new workspace's code. The password is not on
 * this screen — the server keeps only a hash — and the screen says so.
 */

afterEach(() => vi.unstubAllGlobals());

describe("the new workspace's code", () => {
  it("shows the generated code and says the password is not shown again", () => {
    render(<WorkspaceCreated name="Acme" code="K7Q2M9XPAB" onContinue={() => {}} />);

    expect(screen.getByRole("heading", { name: /acme is ready/i })).toBeInTheDocument();
    expect(screen.getByLabelText("Workspace code")).toHaveTextContent("K7Q2M9XPAB");
    expect(screen.getByText(/the password isn.t shown again/i)).toBeInTheDocument();
    expect(screen.getByText(/the code alone opens nothing/i)).toBeInTheDocument();
  });

  it("copies the code", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    render(<WorkspaceCreated name="Acme" code="K7Q2M9XPAB" onContinue={() => {}} />);

    // fireEvent, not userEvent: user-event installs its own clipboard stub.
    fireEvent.click(screen.getByRole("button", { name: /copy code/i }));

    expect(writeText).toHaveBeenCalledWith("K7Q2M9XPAB");
    expect(await screen.findByText("Copied.")).toBeInTheDocument();
  });

  it("says so when the code could not be copied", async () => {
    vi.stubGlobal("navigator", {});
    render(<WorkspaceCreated name="Acme" code="K7Q2M9XPAB" onContinue={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: /copy code/i }));

    expect(await screen.findByText(/couldn.t copy automatically/i)).toBeInTheDocument();
    expect(screen.queryByText("Copied.")).toBeNull();
  });

  it("carries on into the workspace when asked", async () => {
    const onContinue = vi.fn();
    const user = userEvent.setup();
    render(<WorkspaceCreated name="Acme" code="K7Q2M9XPAB" onContinue={onContinue} />);

    await user.click(screen.getByRole("button", { name: /continue to your workspace/i }));

    expect(onContinue).toHaveBeenCalled();
  });
});
