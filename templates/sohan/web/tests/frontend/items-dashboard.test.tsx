// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ItemsDashboard } from "@/components/items-dashboard";
import { makeTestProviders } from "@/tests/helpers/test-providers";

describe("ItemsDashboard", () => {
  it("renders dashboard heading and welcome card", () => {
    const { TestProviders } = makeTestProviders();

    render(
      <TestProviders>
        <ItemsDashboard />
      </TestProviders>,
    );

    expect(screen.getByText("Dashboard")).toBeInTheDocument();
    expect(screen.getByText("Welcome")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Get started" })).toBeInTheDocument();
  });
});
