import { linkSegments } from "../logic/linkSegments";

/** Text with its links made clickable (they open in a new tab). */
export function Linkified({ text }: { text: string }) {
  return (
    <>
      {linkSegments(text).map((segment, index) =>
        segment.href ? (
          <a key={index} href={segment.href} target="_blank" rel="noopener noreferrer">
            {segment.text}
          </a>
        ) : (
          segment.text
        ),
      )}
    </>
  );
}
