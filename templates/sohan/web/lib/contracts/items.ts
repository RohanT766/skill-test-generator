import { z } from "zod";

export const ItemDtoSchema = z.object({
  id: z.string(),
  title: z.string(),
  priceCents: z.number().int().nonnegative(),
  imageUrl: z.string().nullable(),
  createdAt: z.string(),
});

export type ItemDto = z.infer<typeof ItemDtoSchema>;

export const ListItemsInputSchema = z.object({
  query: z.string().default(""),
});

export type ListItemsInput = z.infer<typeof ListItemsInputSchema>;

export const ListItemsOutputSchema = z.array(ItemDtoSchema);

export type ListItemsOutput = z.infer<typeof ListItemsOutputSchema>;

export const CreateItemInputSchema = z.object({
  title: z.string().min(1),
  priceCents: z.number().int().positive(),
  imageUrl: z.string().url().optional(),
});

export type CreateItemInput = z.infer<typeof CreateItemInputSchema>;
export const CreateItemOutputSchema = ItemDtoSchema;
export type CreateItemOutput = z.infer<typeof CreateItemOutputSchema>;

export const GetItemByIdInputSchema = z.object({
  id: z.string().min(1),
});

export type GetItemByIdInput = z.infer<typeof GetItemByIdInputSchema>;
export const GetItemByIdOutputSchema = ItemDtoSchema.nullable();
export type GetItemByIdOutput = z.infer<typeof GetItemByIdOutputSchema>;
