import {
  ArrowDown,
  Archive,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleCheckBig,
  Copy,
  CornerDownRight,
  FastForward,
  FileText,
  LoaderCircle,
  Pencil,
  Play,
  RefreshCw,
  RotateCcw,
  Search,
  Settings2,
  ShieldAlert,
  Telescope,
  TriangleAlert,
  X,
  type LucideIcon,
  type LucideProps,
} from "lucide-react";

const ICONS = {
  "arrow-down": ArrowDown,
  archive: Archive,
  check: Check,
  "check-circle": CheckCircle2,
  "chevron-down": ChevronDown,
  "chevron-right": ChevronRight,
  complete: CircleCheckBig,
  copy: Copy,
  dependency: CornerDownRight,
  replay: FastForward,
  page: FileText,
  loading: LoaderCircle,
  edit: Pencil,
  play: Play,
  refresh: RefreshCw,
  restart: RotateCcw,
  search: Search,
  settings: Settings2,
  boundary: ShieldAlert,
  brand: Telescope,
  warning: TriangleAlert,
  close: X,
} satisfies Record<string, LucideIcon>;

export type IconName = keyof typeof ICONS;

export function Icon({ name, className = "", size = 16, strokeWidth = 1.8, ...props }:
  LucideProps & { name: IconName }) {
  const Glyph = ICONS[name];
  return (
    <Glyph
      className={`ui-icon${className ? ` ${className}` : ""}`}
      size={size}
      strokeWidth={strokeWidth}
      absoluteStrokeWidth
      aria-hidden="true"
      focusable="false"
      {...props}
    />
  );
}
