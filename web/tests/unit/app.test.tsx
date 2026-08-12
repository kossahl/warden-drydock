import { render, screen } from "@testing-library/react";
import { App } from "../../src/App";

describe("accessible application shell", () => {
  it("exposes landmarks, revision, authority, and sync text", () => {
    render(<App />);
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Primary" })).toBeInTheDocument();
    expect(screen.getByRole("main")).toBeInTheDocument();
    expect(screen.getByText("Viewed revision 12 · Head")).toBeInTheDocument();
    expect(screen.getByText("Canon")).toBeInTheDocument();
    expect(screen.getByText("Synced")).toBeInTheDocument();
  });

  it("does not advertise deferred or forbidden product capabilities", () => {
    render(<App />);
    for (const label of ["Import", "Export", "Player", "Billing", "VTT", "Audio"]) {
      expect(screen.queryByRole("link", { name: label })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: label })).not.toBeInTheDocument();
    }
  });
});
