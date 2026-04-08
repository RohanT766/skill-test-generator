import type { z } from "zod";

type MaybePromise<T> = T | Promise<T>;

export function defineServerAction<
  TInputSchema extends z.ZodTypeAny,
  TOutputSchema extends z.ZodTypeAny,
>(options: {
  inputSchema: TInputSchema;
  outputSchema: TOutputSchema;
  handler: (
    input: z.output<TInputSchema>,
  ) => MaybePromise<z.input<TOutputSchema> | z.output<TOutputSchema>>;
}) {
  const { inputSchema, outputSchema, handler } = options;

  return async (
    rawInput: z.input<TInputSchema>,
  ): Promise<z.output<TOutputSchema>> => {
    const input = inputSchema.parse(rawInput);
    const output = await handler(input);
    return outputSchema.parse(output);
  };
}
