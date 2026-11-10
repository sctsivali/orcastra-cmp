import * as React from "react";
import { IllustrationFrame, type IllustrationProps } from "../illustration-frame";
import { Padlock, ServerStack } from "../iso/parts";
import { IsoBox, IsoLink, IsoShadow } from "../iso/primitives";

// Gates and failures, drawn calm: the message and the next step are in the text, and the scene
// only names the situation (locked, or a connection that did not hold).

export function AccessGateIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[26, 18, 107.5, 80.5]}>
      <IsoShadow box={[-14, -6, 28, 12]} />
      <Padlock at={[-14, -6, 0]} />
    </IllustrationFrame>
  );
}

export function DisconnectedIllustration(props: IllustrationProps) {
  const unit = { units: 2, w: 16, d: 16, h: 8, gap: 2 } as const;
  return (
    <IllustrationFrame {...props} view={[19.5, 22.5, 117.5, 88]}>
      <IsoShadow box={[-30, -8, 16, 16]} />
      <IsoShadow box={[14, -8, 16, 16]} />
      <ServerStack at={[-30, -8, 0]} {...unit} />
      <IsoLink path={[[-14, 0, 5], [-11, 0, 5]]} />
      <IsoBox at={[-11, -3, 2]} size={[6, 6, 6]} material="paper" />
      <IsoBox at={[5, -3, 2]} size={[6, 6, 6]} material="ink" />
      <IsoLink path={[[11, 0, 5], [14, 0, 5]]} />
      <ServerStack at={[14, -8, 0]} {...unit} />
    </IllustrationFrame>
  );
}
