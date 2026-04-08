"use client";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export function ItemsDashboard() {
  return (
    <main className="mx-auto flex min-h-screen w-full max-w-5xl flex-col gap-6 px-6 py-10">
      <section>
        <h1 className="text-3xl font-semibold tracking-tight">Dashboard</h1>
        <p className="text-sm text-[hsl(var(--muted-foreground))]">
          Fullstack baseline with Drizzle, pglite tests, TanStack Query, Zustand, and nuqs.
        </p>
      </section>

      <section className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Welcome</CardTitle>
            <CardDescription>This is a minimal Next.js template.</CardDescription>
          </CardHeader>
          <CardContent>
            <Button variant="secondary">Get started</Button>
          </CardContent>
        </Card>
      </section>
    </main>
  );
}
