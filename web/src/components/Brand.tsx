export function BrandMark({ className = "" }: { className?: string }) {
  return (
    <svg className={`evisee-mark ${className}`.trim()} viewBox="0 0 64 64" fill="none" aria-hidden="true">
      <path d="M7 15.8a8 8 0 0 1 7.1-7.9l30-3.4a7 7 0 0 1 7.8 6.2l.2 2.3-31.9 1.9a10.5 10.5 0 0 0-9.8 9.2L7 51.2a7.7 7.7 0 0 1-4-6.8V19.8a4 4 0 0 1 4-4Z" fill="currentColor" />
      <path d="m20 18 33.1-1.7a7.6 7.6 0 0 1 8 7.6v31.2a7.6 7.6 0 0 1-7.6 7.6H15.9a7.6 7.6 0 0 1-7.5-8.5l3.8-28.9A8.4 8.4 0 0 1 20 18Z" fill="currentColor" />
      <path d="M23.6 26h27.7l-.8 7.1H22.8l.8-7.1Zm-2.1 12.5h21.1l-.8 6.9H26.9l-.6 5.1h11.8l4.2 4.3 14.1-14.5 4.8 4.7-17 17.1a2.8 2.8 0 0 1-4 0l-5.1-5.2H19.5l2-18.4Z" fill="white" />
    </svg>
  );
}

export function Brand({ className = "", tagline = false }: { className?: string; tagline?: boolean }) {
  return (
    <div className={`brand evisee-brand ${className}`.trim()}>
      <BrandMark />
      <span className="evisee-wordmark">
        <span className="brand-text">EviSee</span>
        {tagline && <span className="evisee-tagline">See the evidence. Check the source.</span>}
      </span>
    </div>
  );
}
