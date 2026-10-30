"use client";

import { useCallback, useReducer } from "react";
import { joinPath, parentOf } from "@/lib/instance-files/path";

export type SortField = "name" | "size" | "modified" | "owner" | "mode";
export type SortDirection = "asc" | "desc";

export interface OpenFile {
  path: string;
  kind: "text" | "image";
}

export interface SessionState {
  path: string;
  back: string[];
  forward: string[];
  selected: ReadonlySet<string>;
  anchor: string | null;
  active: string | null;
  /** The row to focus after a listing arrives, ex; the folder just left. */
  focusAfterLoad: string | null;
  filter: string;
  showHidden: boolean;
  sortField: SortField;
  sortDirection: SortDirection;
  open: OpenFile | null;
}

type Action =
  | { type: "navigate"; path: string; focus?: string | null }
  | { type: "back" }
  | { type: "forward" }
  | { type: "select"; names: string[]; anchor?: string | null; active?: string | null }
  | { type: "active"; name: string | null }
  | { type: "focused" }
  | { type: "filter"; value: string }
  | { type: "hidden"; value: boolean }
  | { type: "sort"; field: SortField }
  | { type: "prune"; visible: ReadonlySet<string> }
  | { type: "open"; file: OpenFile | null };

const EMPTY: ReadonlySet<string> = new Set<string>();

function reducer(state: SessionState, action: Action): SessionState {
  switch (action.type) {
    case "navigate":
      if (action.path === state.path) return { ...state, open: null, focusAfterLoad: action.focus ?? null };
      return {
        ...state,
        path: action.path,
        back: [...state.back, state.path],
        forward: [],
        selected: EMPTY,
        anchor: null,
        active: null,
        filter: "",
        open: null,
        focusAfterLoad: action.focus ?? null,
      };
    case "back": {
      const target = state.back[state.back.length - 1];
      if (target === undefined) return state;
      return {
        ...state,
        path: target,
        back: state.back.slice(0, -1),
        forward: [state.path, ...state.forward],
        selected: EMPTY,
        anchor: null,
        active: null,
        filter: "",
        open: null,
      };
    }
    case "forward": {
      const target = state.forward[0];
      if (target === undefined) return state;
      return {
        ...state,
        path: target,
        back: [...state.back, state.path],
        forward: state.forward.slice(1),
        selected: EMPTY,
        anchor: null,
        active: null,
        filter: "",
        open: null,
      };
    }
    case "select":
      return {
        ...state,
        selected: new Set(action.names),
        anchor: action.anchor === undefined ? state.anchor : action.anchor,
        active: action.active === undefined ? state.active : action.active,
      };
    case "active":
      return { ...state, active: action.name };
    case "focused":
      return { ...state, focusAfterLoad: null };
    case "filter":
      return { ...state, filter: action.value };
    case "hidden":
      return { ...state, showHidden: action.value };
    case "sort":
      return state.sortField === action.field
        ? { ...state, sortDirection: state.sortDirection === "asc" ? "desc" : "asc" }
        : { ...state, sortField: action.field, sortDirection: "asc" };
    case "prune": {
      // Nothing unseen may be acted on: a filter that hides a selected row also unselects it.
      const kept = [...state.selected].filter((name) => action.visible.has(name));
      return kept.length === state.selected.size ? state : { ...state, selected: new Set(kept) };
    }
    case "open":
      return { ...state, open: action.file };
  }
}

export function useExplorerSession(initialPath: string) {
  const [state, dispatch] = useReducer(reducer, {
    path: initialPath,
    back: [],
    forward: [],
    selected: EMPTY,
    anchor: null,
    active: null,
    focusAfterLoad: null,
    filter: "",
    showHidden: false,
    sortField: "name",
    sortDirection: "asc",
    open: null,
  } satisfies SessionState);

  const navigate = useCallback((path: string, focus?: string | null) => dispatch({ type: "navigate", path, focus }), []);
  const goUp = useCallback(() => {
    if (state.path === "/") return;
    dispatch({ type: "navigate", path: parentOf(state.path), focus: state.path.slice(state.path.lastIndexOf("/") + 1) });
  }, [state.path]);
  const enter = useCallback((name: string) => dispatch({ type: "navigate", path: joinPath(state.path, name) }), [state.path]);

  return { state, dispatch, navigate, goUp, enter };
}
