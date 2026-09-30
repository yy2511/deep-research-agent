import {
  ArrowDown,
  ArrowRight,
  ArrowUp,
  Archive,
  BookOpen,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleCheckBig,
  Copy,
  CornerDownRight,
  FastForward,
  FileText,
  Globe,
  History,
  Info,
  LoaderCircle,
  Menu,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Settings2,
  ShieldAlert,
  SlidersHorizontal,
  Sparkles,
  Square,
  Telescope,
  TriangleAlert,
  Upload,
  X,
  type LucideIcon,
  type LucideProps,
} from "lucide-react";

const ICONS = {
  "arrow-down": ArrowDown,
  "arrow-right": ArrowRight,
  "arrow-up": ArrowUp,
  archive: Archive,
  book: BookOpen,
  check: Check,
  "check-circle": CheckCircle2,
  "chevron-down": ChevronDown,
  "chevron-right": ChevronRight,
  complete: CircleCheckBig,
  copy: Copy,
  dependency: CornerDownRight,
  globe: Globe,
  history: History,
  info: Info,
  replay: FastForward,
  page: FileText,
  loading: LoaderCircle,
  edit: Pencil,
  menu: Menu,
  play: Play,
  plus: Plus,
  refresh: RefreshCw,
  restart: RotateCcw,
  search: Search,
  settings: Settings2,
  sliders: SlidersHorizontal,
  sparkles: Sparkles,
  stop: Square,
  boundary: ShieldAlert,
  brand: Telescope,
  upload: Upload,
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
