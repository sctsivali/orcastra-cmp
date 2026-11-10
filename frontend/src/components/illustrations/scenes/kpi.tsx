import * as React from "react";
import { IllustrationFrame, type IllustrationProps } from "../illustration-frame";
import { ARROW_DOWN, ARROW_UP, Certificate, Key, Monitor, ServerStack, StatusToken, type TokenGlyph } from "../iso/parts";
import { IsoBox, IsoExtrude, IsoFlat, IsoGlyph, IsoInset, IsoShadow } from "../iso/primitives";
import { circleOutline } from "../iso/projection";

// KPI tiles show these at 64px, so each is one or two large forms and its fine detail drops out.
// A scene names the tile's subject; the number beside it is the only thing that changes.

export function ClustersIllustration(props: IllustrationProps) {
  // The second cluster stands half a step behind the first so the pair reads as one form.
  const unit = { units: 2, w: 24, d: 16, h: 8, gap: 2 } as const;
  return (
    <IllustrationFrame {...props} view={[22.5, 19, 114.5, 86]}>
      <IsoShadow box={[-8.5, -21, 24, 16]} />
      <IsoShadow box={[-15.5, 5, 24, 16]} />
      <ServerStack at={[-8.5, -21, 0]} hero={1} {...unit} />
      <ServerStack at={[-15.5, 5, 0]} {...unit} />
    </IllustrationFrame>
  );
}

export function InstancesIllustration(props: IllustrationProps) {
  const face = [-28, 14, 0] as const;
  return (
    <IllustrationFrame {...props} view={[18, -1, 128.5, 96.5]}>
      <IsoShadow box={[6, -26, 14, 14]} />
      <IsoShadow box={[-28, -6, 26, 20]} />
      <IsoBox at={[6, -26, 0]} size={[14, 14, 14]} material="tint" />
      <IsoBox at={[6, -26, 14]} size={[14, 14, 14]} material="ink" />
      <IsoBox at={[-28, -6, 0]} size={[26, 20, 26]} material="paper" />
      <IsoInset plane="left" origin={face} rect={[3.5, 8, 19, 13]} kind="panel" />
    </IllustrationFrame>
  );
}

const BLOCK = { at: [-20, -15, 0] as const, size: [40, 30, 24] as const };
const FRONT = [BLOCK.at[0], BLOCK.at[1] + BLOCK.size[1], 0] as const;

export function RunningIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[14, 10.5, 132.5, 99.5]}>
      <IsoShadow box={[-20, -15, 40, 30]} />
      <IsoBox at={BLOCK.at} size={BLOCK.size} material="tint" />
      {/* The apex is lifted by half its run to undo the face's skew, so the mark reads as a play
          sign at 64px instead of a corner wedge. */}
      <IsoFlat plane="left" origin={FRONT} outline={[[15, 4], [15, 20], [29, 19]]} kind="detail" />
    </IllustrationFrame>
  );
}

export function StoppedIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[14, 10.5, 132.5, 99.5]}>
      <IsoShadow box={[-20, -15, 40, 30]} />
      <IsoBox at={BLOCK.at} size={BLOCK.size} material="tint" />
      <IsoInset plane="left" origin={FRONT} rect={[13, 5, 5, 14]} kind="detail" />
      <IsoInset plane="left" origin={FRONT} rect={[22, 5, 5, 14]} kind="detail" />
    </IllustrationFrame>
  );
}

/** A warning sign on its post, in front of the server it is about. */
export function WarningsIllustration(props: IllustrationProps) {
  const sign = [-11, 6, 12] as const;
  return (
    <IllustrationFrame {...props} view={[18.5, 15, 117.5, 88]}>
      <IsoShadow box={[-30, -20, 24, 16]} />
      <IsoShadow box={[-4, -2, 20, 12]} />
      <ServerStack at={[-30, -20, 0]} units={2} w={24} d={16} h={8} />
      <IsoBox at={[-4, -2, 0]} size={[20, 12, 3]} material="paper" />
      <IsoBox at={[4.5, 2.5, 3]} size={[3, 3, 9]} material="paper" />
      <IsoExtrude plane="left" origin={sign} outline={[[0, 0], [34, 0], [17, 29]]} depth={4} material="tint" />
      <IsoInset plane="left" origin={sign} rect={[15.2, 10, 3.6, 11]} kind="detail" />
      <IsoInset plane="left" origin={sign} rect={[15.2, 3.5, 3.6, 3.6]} kind="detail" />
    </IllustrationFrame>
  );
}

// Key and certificate tiles: the thing itself, and for a status tile a token in front of it. The
// helpers draw the objects; each scene keeps its own frame and measured view.

function KeyStatus({ glyph }: { glyph: TokenGlyph }) {
  return (
    <>
      <IsoShadow box={[-22, -14, 36, 24]} />
      <IsoShadow box={[6, 11, 18.4, 3]} />
      <IsoBox at={[-22, -14, 0]} size={[36, 24, 5]} material="paper" />
      <Key origin={[-13, 0, 2.5]} material="tint" scale={0.62} />
      <StatusToken origin={[6, 14, 0]} glyph={glyph} />
    </>
  );
}

export function KeysActiveIllustration(props: IllustrationProps) {
  return <IllustrationFrame {...props} view={[18.5, 19, 118.5, 89]}><KeyStatus glyph="check" /></IllustrationFrame>;
}

export function KeysExpiredIllustration(props: IllustrationProps) {
  return <IllustrationFrame {...props} view={[18.5, 19, 118.5, 89]}><KeyStatus glyph="clock" /></IllustrationFrame>;
}

export function KeysRevokedIllustration(props: IllustrationProps) {
  return <IllustrationFrame {...props} view={[18.5, 19, 118.5, 89]}><KeyStatus glyph="cross" /></IllustrationFrame>;
}

/** Certificates held for the fleet: one behind another. */
export function CertificatesIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[17, 7.5, 126.5, 95]}>
      <IsoShadow box={[-20, -9, 26, 3]} />
      <IsoShadow box={[-8, 3, 26, 3]} />
      <Certificate origin={[-20, -6, 0]} />
      <Certificate origin={[-8, 6, 0]} material="tint" />
    </IllustrationFrame>
  );
}

function CertificateStatus({ glyph }: { glyph: TokenGlyph }) {
  return (
    <>
      <IsoShadow box={[-18, -1, 26, 3]} />
      <IsoShadow box={[4, 9, 18.4, 3]} />
      <Certificate origin={[-18, 2, 0]} />
      <StatusToken origin={[4, 12, 0]} glyph={glyph} />
    </>
  );
}

export function CertificatesValidIllustration(props: IllustrationProps) {
  return <IllustrationFrame {...props} view={[15.5, 14, 122, 91.5]}><CertificateStatus glyph="check" /></IllustrationFrame>;
}

export function CertificatesExpiringIllustration(props: IllustrationProps) {
  return <IllustrationFrame {...props} view={[15.5, 14, 122, 91.5]}><CertificateStatus glyph="clock" /></IllustrationFrame>;
}

export function CertificatesExpiredIllustration(props: IllustrationProps) {
  return <IllustrationFrame {...props} view={[15.5, 14, 122, 91.5]}><CertificateStatus glyph="cross" /></IllustrationFrame>;
}

// Session tiles: a console open on a screen, and data leaving or arriving.

export function SessionsActiveIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[27.5, 13, 105, 79]}>
      <IsoShadow box={[-6, -8, 12, 12]} />
      <Monitor at={[-20, -2, 0]} mark="prompt" />
    </IllustrationFrame>
  );
}

function Tray() {
  return (
    <>
      <IsoShadow box={[-17, -13, 34, 26]} />
      <IsoBox at={[-17, -13, 0]} size={[34, 26, 6]} material="paper" open />
    </>
  );
}

export function DownloadIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[25.5, 23, 109, 82]}>
      <Tray />
      <IsoExtrude plane="left" origin={[-10, 2, 4]} outline={ARROW_DOWN} depth={4} material="ink" />
    </IllustrationFrame>
  );
}

export function UploadIllustration(props: IllustrationProps) {
  return (
    <IllustrationFrame {...props} view={[27.5, 25.5, 105, 79]}>
      <Tray />
      <IsoExtrude plane="left" origin={[-10, 2, 6]} outline={ARROW_UP} depth={4} material="ink" />
    </IllustrationFrame>
  );
}

const DIAL_FACE = circleOutline(14, 16, 14, 32);

/** A clock on its stand, for time spent in sessions. */
export function DurationIllustration(props: IllustrationProps) {
  const face = [-14, 4, 4] as const;
  return (
    <IllustrationFrame {...props} view={[28.5, 24.5, 101, 75.5]}>
      <IsoShadow box={[-16, -6, 32, 14]} />
      <IsoBox at={[-16, -6, 0]} size={[32, 14, 4]} material="paper" />
      <IsoExtrude plane="left" origin={face} outline={DIAL_FACE} depth={5} material="ink" />
      {[0, 90, 180, 270].map((deg) => {
        const t = (deg * Math.PI) / 180;
        return <IsoGlyph key={deg} plane="left" origin={face} path={[[14 + Math.cos(t) * 10, 16 + Math.sin(t) * 10], [14 + Math.cos(t) * 12, 16 + Math.sin(t) * 12]]} onInk />;
      })}
      <IsoGlyph plane="left" origin={face} path={[[14, 24], [14, 16], [20, 12.5]]} onInk />
    </IllustrationFrame>
  );
}
