import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import HomePage from "../src/app/page";

describe("landing placeholder", () => {
  it("renders one top-level heading", () => {
    render(<HomePage />);
    expect(
      screen.getByRole("heading", { level: 1, name: /dashboard scaffold ready/i }),
    ).toBeDefined();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it("states the scaffold facts without claiming dashboard functionality", () => {
    render(<HomePage />);
    expect(screen.getByText(/next\.js 16/i)).toBeDefined();
    expect(screen.getByText(/typescript 5\.9/i)).toBeDefined();
    expect(screen.getByText(/css modules/i)).toBeDefined();
    // No invented data and no navigation yet.
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(screen.queryByText(/fixture/i)?.textContent ?? "").not.toContain(
      "FIXTURE DATA",
    );
  });
});
