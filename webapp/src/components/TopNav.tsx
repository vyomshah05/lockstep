export type SectionKey = 'claude-code' | 'devin' | 'add-docs';

const SECTIONS: { key: SectionKey; label: string; activeClass: string }[] = [
  { key: 'claude-code', label: 'Claude Code', activeClass: 'text-claude dark:text-claude-dark bg-claude/10 dark:bg-claude-dark/15' },
  { key: 'devin', label: 'Devin', activeClass: 'text-accent dark:text-accent-dark bg-accent/10 dark:bg-accent-dark/15' },
  { key: 'add-docs', label: 'Add Documentation', activeClass: 'text-teal dark:text-teal-dark bg-teal/10 dark:bg-teal-dark/15' },
];

interface TopNavProps {
  active: SectionKey;
  onChange: (key: SectionKey) => void;
}

export function TopNav({ active, onChange }: TopNavProps) {
  return (
    <nav className="flex items-center gap-1">
      {SECTIONS.map((s) => {
        const isActive = s.key === active;
        return (
          <button
            key={s.key}
            onClick={() => onChange(s.key)}
            className={`relative rounded-lg px-3 py-2 text-sm font-medium transition ${
              isActive
                ? s.activeClass
                : 'text-slate-600 dark:text-slate-300 hover:text-slate-900 dark:hover:text-white hover:bg-black/5 dark:hover:bg-white/10'
            }`}
          >
            {s.label}
          </button>
        );
      })}
    </nav>
  );
}
