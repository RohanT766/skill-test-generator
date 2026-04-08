import { create } from "zustand";

type UiState = {
  isComposerOpen: boolean;
  openComposer: () => void;
  closeComposer: () => void;
};

export const useUiStore = create<UiState>((set) => ({
  isComposerOpen: false,
  openComposer: () => set({ isComposerOpen: true }),
  closeComposer: () => set({ isComposerOpen: false }),
}));
