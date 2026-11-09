import * as React from "react";
import { IllustrationFrame, type IllustrationProps } from "../illustration-frame";
import { Chip, DiskStack, GpuCard, RamStick } from "../iso/parts";
import { IsoGround } from "../iso/primitives";

// Host hardware, for the Technologies summary cards (CPU, Memory, Storage, GPU).

export function CpuIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props}>
      <IsoGround />
      <Chip at={[-16, -16, 0]} size={32} />
    </IllustrationFrame>
  );
}

export function MemoryIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props}>
      <IsoGround />
      <RamStick at={[-27, -11, 0]} />
      <RamStick at={[-27, 2, 0]} material="ink" />
    </IllustrationFrame>
  );
}

export function StorageIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props}>
      <IsoGround />
      <DiskStack at={[0, 0, 0]} radius={17} height={7} />
    </IllustrationFrame>
  );
}

export function GpuIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props}>
      <IsoGround />
      <GpuCard at={[-26, -6, 0]} />
    </IllustrationFrame>
  );
}
