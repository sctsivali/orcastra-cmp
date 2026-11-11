"use client";

import * as React from "react";
import { byteFormat, type ByteFormat } from "@/lib/byte-format";
import { DEFAULT_BYTE_UNITS, type ByteUnitSystem } from "@/lib/byte-units";

/**
 * The account's byte unit system. PersonalizationProvider fills it from the hint cookie on the
 * server render and from the account's saved choice afterwards, so the server and the first client
 * render print the same text. A separate context from the personalization one, so a token rotation
 * or a branding change does not re-render every component that prints a size.
 */
export const ByteUnitsContext = React.createContext<ByteUnitSystem>(DEFAULT_BYTE_UNITS);

/** The byte formatters in the account's unit system. */
export function useByteFormat(): ByteFormat {
  return byteFormat(React.useContext(ByteUnitsContext));
}

/**
 * The account's system as a ref, for a form that reads it once, when its values load. A switch made
 * in another tab while the form is open then never swaps the units under what the operator types,
 * and the effect that loads the values does not re-run because of it.
 */
export function useByteUnitsRef(): React.MutableRefObject<ByteUnitSystem> {
  const units = React.useContext(ByteUnitsContext);
  const ref = React.useRef(units);
  ref.current = units;
  return ref;
}
