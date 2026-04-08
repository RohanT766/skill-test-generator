import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { NuqsTestingAdapter } from "nuqs/adapters/testing";
import type { ReactNode } from "react";

type TestProvidersProps = {
  children: ReactNode;
};

export function makeTestProviders(searchParams: string = "") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 0 },
      mutations: { retry: false },
    },
  });

  function TestProviders({ children }: TestProvidersProps) {
    return (
      <NuqsTestingAdapter searchParams={searchParams}>
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
      </NuqsTestingAdapter>
    );
  }

  return { TestProviders, queryClient };
}
