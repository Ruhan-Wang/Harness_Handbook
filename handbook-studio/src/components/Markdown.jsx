import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

export default function Markdown({ children }) {
  return (
    <div className="prose-invert max-w-none text-sm leading-relaxed text-slate-200">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: (p) => <h1 className="mb-3 mt-5 text-xl font-semibold text-foreground" {...p} />,
          h2: (p) => <h2 className="mb-2 mt-5 text-lg font-semibold text-foreground" {...p} />,
          h3: (p) => <h3 className="mb-2 mt-4 text-base font-semibold text-slate-100" {...p} />,
          h4: (p) => <h4 className="mb-1 mt-3 text-sm font-semibold text-slate-100" {...p} />,
          p: (p) => <p className="my-2 text-slate-300" {...p} />,
          ul: (p) => <ul className="my-2 list-disc space-y-1 pl-5 text-slate-300" {...p} />,
          ol: (p) => <ol className="my-2 list-decimal space-y-1 pl-5 text-slate-300" {...p} />,
          li: (p) => <li className="text-slate-300" {...p} />,
          code: ({ inline, ...p }) =>
            inline ? (
              <code className="code-font rounded bg-ink-700 px-1 py-0.5 text-[0.85em] text-amber-200" {...p} />
            ) : (
              <code className="code-font block overflow-x-auto rounded-md bg-ink-900 p-3 text-[0.85em] text-slate-200" {...p} />
            ),
          table: (p) => (
            <div className="my-3 overflow-x-auto">
              <table className="w-full border-collapse text-xs" {...p} />
            </div>
          ),
          th: (p) => <th className="border border-ink-600 bg-ink-700 px-2 py-1 text-left" {...p} />,
          td: (p) => <td className="border border-ink-600 px-2 py-1 align-top" {...p} />,
          a: (p) => <a className="text-accent underline" {...p} />,
        }}
      >
        {children || ''}
      </ReactMarkdown>
    </div>
  );
}
