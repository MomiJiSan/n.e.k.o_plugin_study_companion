export type PracticeScopeDisplaySource = {
  stage?: unknown;
  subject?: unknown;
  display_path?: unknown;
};

export type PracticeScopeTranslator = (key: string, fallback: string) => string;

export function practiceScopeDisplayPath(
  scope: PracticeScopeDisplaySource | null | undefined,
  translate: PracticeScopeTranslator,
): string {
  const path = Array.isArray(scope?.display_path)
    ? scope.display_path.map((part) => String(part || '').trim()).filter(Boolean)
    : [];
  const stage = String(scope?.stage || '').trim();
  const subject = String(scope?.subject || '').trim();
  if (path.length && stage) {
    path[0] = translate(`ui.profile.stage.${stage}`, stage.replaceAll('_', ' '));
  }
  if (path.length > 1 && subject) {
    path[1] = translate(`ui.knowledge.subject.${subject}`, subject.replaceAll('_', ' '));
  }
  return path.join(' / ');
}
