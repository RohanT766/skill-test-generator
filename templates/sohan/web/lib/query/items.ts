// TEMPLATE REFERENCE — this file is a scaffold showing the expected pattern for
// query hooks that call typed server actions. Concrete imports are commented out
// because the db_schema agent rewrites db/schema.ts and backend_builder rewrites
// action modules. Keeping this disconnected avoids template breakage.
//
// import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
// import { type CreateItemInput } from "@/lib/contracts/items";
// import { createItemAction, listItemsAction } from "@/lib/server/actions/items";
//
// const queryKeys = {
//   items: (query: string) => ["items", query] as const,
// };
//
// export function useItems(query: string) {
//   return useQuery({
//     queryKey: queryKeys.items(query),
//     queryFn: () => listItemsAction({ query }),
//   });
// }
//
// export function useCreateItem(query: string) {
//   const client = useQueryClient();
//
//   return useMutation({
//     mutationFn: (input: CreateItemInput) => createItemAction(input),
//     onSuccess: async () => {
//       await client.invalidateQueries({ queryKey: queryKeys.items(query) });
//     },
//   });
// }

export {};
