/** A labelled stand-in for a photograph that will be supplied later. */
export default function Slot({ what, shows, format }: { what: string; shows: string; format: string }) {
  return (
    <div className="slot" role="img" aria-label={`Image placeholder: ${what}. ${shows}. ${format}.`}>
      <b>{what}</b>
      <span>{shows}</span>
      <span>{format}</span>
    </div>
  );
}
