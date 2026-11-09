import * as React from "react";
import { IllustrationFrame, type IllustrationProps } from "../illustration-frame";
import { Chip, DiskStack, GpuCard, RamStick } from "../iso/parts";
import { IsoShadow } from "../iso/primitives";

// Host hardware, for the Technologies summary cards (CPU, Memory, Storage, GPU).

export function CpuIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[32.5, 34, 94.5, 71]}>
      <IsoShadow box={[-16, -16, 36, 36]} />
      <Chip at={[-16, -16, 0]} size={32} />
    </IllustrationFrame>
  );
}

export function MemoryIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[28.5, 24, 109.5, 82]}>
      <IsoShadow box={[-27, -11, 54, 16]} />
      <RamStick at={[-27, -11, 0]} />
      <RamStick at={[-27, 2, 0]} material="tint" />
    </IllustrationFrame>
  );
}

export function StorageIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[22, 14.5, 116, 87]}>
      <IsoShadow circle={[0, 0, 17]} />
      <DiskStack at={[0, 0, 0]} radius={17} height={7} />
    </IllustrationFrame>
  );
}

export function GpuIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[13, 10.5, 130, 97.5]}>
      <IsoShadow box={[-29, -7, 55, 14]} />
      <GpuCard at={[-26, -6, 0]} />
    </IllustrationFrame>
  );
}
