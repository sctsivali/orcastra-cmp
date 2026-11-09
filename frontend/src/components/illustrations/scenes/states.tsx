import * as React from "react";
import { IllustrationFrame, type IllustrationProps } from "../illustration-frame";
import { Padlock, ServerStack } from "../iso/parts";
import { IsoBox, IsoGround, IsoLink } from "../iso/primitives";

// Gates and failures. Calm on purpose: the message and the next step are in the text, the scene
// only names the situation (locked, or a connection that did not hold).

export function AccessGateIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props}>
      <IsoGround />
      <Padlock at={[-14, -6, 0]} />
    </IllustrationFrame>
  );
}

export function DisconnectedIllustration(props: IllustrationProps) {
  const unit = { units: 2, w: 16, d: 16, h: 8, gap: 2 } as const;
  return (
    <IllustrationFrame {...props}>
      <IsoGround />
      <ServerStack at={[-30, -8, 0]} {...unit} />
      <IsoLink path={[[-14, 0, 5], [-11, 0, 5]]} />
      <IsoBox at={[-11, -3, 2]} size={[6, 6, 6]} material="paper" />
      <IsoBox at={[5, -3, 2]} size={[6, 6, 6]} material="ink" />
      <IsoLink path={[[11, 0, 5], [14, 0, 5]]} />
      <ServerStack at={[14, -8, 0]} {...unit} />
    </IllustrationFrame>
  );
}
