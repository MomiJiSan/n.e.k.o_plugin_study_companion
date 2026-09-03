import { useEffect, useRef, useState } from '@neko/plugin-ui';
import type { PluginSurfaceProps } from '@neko/plugin-ui';

import {
  callPlugin,
  ensureBrandCSS,
  postStudySurfaceMessage,
  STUDY_SURFACE_MESSAGE_TYPES,
} from './study_surface_utils';
import {
  isKnowledgeMapCursorStale,
  knowledgeMapErrorMessage,
  mergeKnowledgeMapPage,
} from './study_ui_contracts';

type KnowledgeNode = {
  id: string;
  label: string;
  stage?: string;
  subject?: string;
  course_family?: string;
  chapter?: string;
  unit?: string;
  mastery?: number;
  mastery_status?: string;
  assessed?: boolean;
  level?: string;
  weak?: boolean;
  /** Included only to explain a selected graph range; not part of that range. */
  boundary?: boolean;
  in_scope?: boolean;
  question_types?: string[];
  typical_misconceptions?: string[];
};

type PracticeScope = {
  schema_version?: number;
  mode?: 'explicit_scope' | 'explicit_topic';
  stage?: string;
  subject?: string;
  course_family?: string;
  chapter?: string;
  unit?: string;
  topic_id?: string;
  scope_key?: string;
  scope_revision?: number;
  display_path?: string[];
};

type KnowledgeEdge = {
  from: string;
  to: string;
  relation?: string;
  reason?: string;
  reason_template?: string;
  priority?: string;
  context?: string;
  confidence?: number;
};

type KnowledgeMapPage = {
  nodes?: KnowledgeNode[];
  edges?: KnowledgeEdge[];
  summary?: Record<string, number>;
  catalog_revision?: string;
  has_more?: boolean;
  next_cursor?: string;
  subject_counts?: Record<string, number>;
  edge_truncated?: boolean;
  boundary?: { truncated?: boolean; returned_count?: number; [key: string]: unknown };
  [key: string]: unknown;
};

const KNOWLEDGE_SUBJECT_OPTIONS = ['math', 'english', 'chinese', 'physics', 'chemistry', 'biology', 'history', 'geography', 'politics', 'computer_science', 'economics'];
const LEARNING_PROFILE_STORAGE_KEY = 'study_companion.learning_profile.v1';
const LEARNING_STAGE_OPTIONS = ['primary', 'junior_high', 'senior_high', 'college', 'cross_stage', 'postgraduate', 'custom'];

function normalizeLearningStage(value: unknown) {
  const normalized = String(value || '').trim().toLowerCase().replaceAll('-', '_');
  return LEARNING_STAGE_OPTIONS.includes(normalized) ? normalized : '';
}

function readDefaultLearningStage() {
  try {
    const profile = JSON.parse(window.localStorage?.getItem(LEARNING_PROFILE_STORAGE_KEY) || '{}') || {};
    return normalizeLearningStage(profile.stage);
  } catch {
    return '';
  }
}

function writeDefaultLearningStage(stage: string) {
  const normalized = normalizeLearningStage(stage);
  if (!normalized) return '';
  try {
    const profile = JSON.parse(window.localStorage?.getItem(LEARNING_PROFILE_STORAGE_KEY) || '{}') || {};
    window.localStorage?.setItem(LEARNING_PROFILE_STORAGE_KEY, JSON.stringify({
      ...profile,
      stage: normalized,
      skipped: false,
      completed: true,
    }));
  } catch {
    return '';
  }
  return normalized;
}

function knowledgeMapScope(stage: string, subject = 'all') {
  return {
    stage: stage === 'all' ? '' : normalizeLearningStage(stage),
    subject: subject === 'all' ? '' : String(subject || '').trim(),
    chapter: '',
    unit: '',
  };
}

async function loadKnowledgeMapPage(
  api: PluginSurfaceProps['api'],
  stage: string,
  subject: string,
  current: KnowledgeMapPage | null,
): Promise<KnowledgeMapPage> {
  let retryAfterStaleCursor = true;
  let activePayload = current;
  while (true) {
    const cursor = activePayload ? String(activePayload.next_cursor || '').trim() : '';
    if (activePayload?.has_more === true && !cursor) {
      throw new Error('Knowledge map pagination returned a repeated cursor.');
    }
    try {
      const page = await callPlugin(api, 'study_query_knowledge_map', {
        scope: knowledgeMapScope(stage, subject),
        page_size: 100,
        cursor,
        include_boundary: true,
      }) as KnowledgeMapPage;
      if (page?.error) throw page.error;
      if (page?.has_more === true) {
        const nextCursor = String(page?.next_cursor || '').trim();
        if (!nextCursor || nextCursor === cursor) {
          throw new Error('Knowledge map pagination returned a repeated cursor.');
        }
      }
      const expectedRevision = String(activePayload?.catalog_revision || '').trim();
      const pageRevision = String(page?.catalog_revision || '').trim();
      if (activePayload && expectedRevision && pageRevision && expectedRevision !== pageRevision) {
        if (!retryAfterStaleCursor) throw new Error('Knowledge map catalog revision changed while loading.');
        retryAfterStaleCursor = false;
        activePayload = null;
        continue;
      }
      return mergeKnowledgeMapPage(activePayload, page) as KnowledgeMapPage;
    } catch (error) {
      if (retryAfterStaleCursor && isKnowledgeMapCursorStale(error)) {
        retryAfterStaleCursor = false;
        activePayload = null;
        continue;
      }
      throw error;
    }
  }
}

function text(props: PluginSurfaceProps, key: string, fallback: string) {
  const value = props.t?.(key);
  return value && value !== key ? value : fallback;
}

function formatText(
  props: PluginSurfaceProps,
  key: string,
  fallback: string,
  values: Record<string, string | number>,
): string {
  const translated = props.t?.(key, values);
  if (translated && translated !== key) return translated;
  return fallback.replace(/\{([^}]+)\}/g, (_, name: string) => String(values[name] ?? ''));
}

function nodeIsAssessed(node: KnowledgeNode) {
  if (node.assessed === false) return false;
  const status = String(node.mastery_status || '').trim().toLowerCase();
  if (['unassessed', 'insufficient_evidence', 'new'].includes(status)) return false;
  if (status) return true;
  if (node.assessed === true || node.weak) return true;
  const level = String(node.level || '').trim().toLowerCase();
  if (['weak', 'progress', 'good', 'mastered'].includes(level)) return true;
  if (level === 'new') return false;
  return typeof node.mastery === 'number' && Number.isFinite(node.mastery);
}

function nodeMasteryLevel(node: KnowledgeNode) {
  if (!nodeIsAssessed(node)) return 'new';
  const status = String(node.mastery_status || '').trim().toLowerCase();
  const statusLevels: Record<string, string> = {
    mastered: 'mastered',
    good: 'good',
    progressing: 'progress',
    progress: 'progress',
    weak: 'weak',
  };
  if (statusLevels[status]) return statusLevels[status];
  if (node.weak) return 'weak';
  const level = String(node.level || '').trim().toLowerCase();
  if (['new', 'weak', 'progress', 'good', 'mastered'].includes(level)) return level;
  const mastery = Number(node.mastery);
  if (!Number.isFinite(mastery)) return 'new';
  if (mastery >= 0.85) {
    return 'mastered';
  }
  if (mastery >= 0.6) {
    return 'good';
  }
  if (mastery >= 0.3) {
    return 'progress';
  }
  return 'weak';
}

function nodeMasteryText(props: PluginSurfaceProps, node: KnowledgeNode) {
  if (!nodeIsAssessed(node)) {
    return ` ${text(props, 'ui.knowledge.mastery.unassessed', 'Unassessed')}`;
  }
  const mastery = typeof node.mastery === 'number' && Number.isFinite(node.mastery)
    ? node.mastery
    : null;
  return mastery === null ? '' : ` ${Math.round(mastery * 100)}%`;
}

function nodeIsWeakTopic(node: KnowledgeNode) {
  if (!nodeIsAssessed(node)) return false;
  const hasStatus = String(node.mastery_status || '').trim() !== '';
  const hasAssessed = typeof node.assessed === 'boolean';
  // `weak` is a practice-priority flag, not always the visual mastery level:
  // a progressing 0.40–0.59 node can still be weak.
  if (hasStatus || hasAssessed) return node.weak === true;
  return nodeMasteryLevel(node) === 'weak';
}

function nodeLabel(node?: Partial<KnowledgeNode>) {
  return String(node?.label || node?.id || '-');
}

function isBoundaryNode(node?: Partial<KnowledgeNode>) {
  return node?.boundary === true || node?.in_scope === false;
}

function nodeSubject(node?: Partial<KnowledgeNode>) {
  return String(node?.subject || '').trim();
}

function subjectLabel(props: PluginSurfaceProps, subject: string) {
  const normalized = String(subject || '').trim();
  if (!normalized) return text(props, 'ui.knowledge.subject_uncategorized', 'Uncategorized subject');
  return text(props, `ui.knowledge.subject.${normalized}`, normalized.replace(/_/g, ' '));
}

function stageLabel(props: PluginSurfaceProps, stage: string) {
  const normalized = String(stage || '').trim();
  if (!normalized) return text(props, 'ui.knowledge.stage_uncategorized', 'Uncategorized stage');
  return text(props, `ui.knowledge.stage.${normalized}`, normalized.replace(/_/g, ' '));
}

function valueLabel(value: string) {
  return String(value || '').trim().replace(/_/g, ' ');
}

function uniqueValues(nodes: KnowledgeNode[], field: keyof KnowledgeNode) {
  return Array.from(new Set(nodes.map((node) => String(node[field] || '').trim()).filter(Boolean)))
    .sort((left, right) => left.localeCompare(right));
}

function relationLabel(props: PluginSurfaceProps, relation?: string) {
  const normalized = String(relation || 'related').trim().toLowerCase();
  if (normalized === 'prerequisite') return text(props, 'ui.knowledge.edge_relation.prerequisite', 'Prerequisite');
  if (normalized === 'application') return text(props, 'ui.knowledge.edge_relation.application', 'Application');
  if (normalized === 'procedure_step') return text(props, 'ui.knowledge.edge_relation.procedure_step', 'Procedure Step');
  if (normalized === 'confusable') return text(props, 'ui.knowledge.edge_relation.confusable', 'Confusable');
  if (normalized === 'co_occurs') return text(props, 'ui.knowledge.edge_relation.co_occurs', 'Co-occurs');
  if (normalized === 'supports') return text(props, 'ui.knowledge.edge_relation.supports', 'Supports');
  if (normalized === 'analogy') return text(props, 'ui.knowledge.edge_relation.analogy', 'Analogy');
  if (normalized === 'related') return text(props, 'ui.knowledge.edge_relation.related', 'Related');
  if (normalized === 'similar') return text(props, 'ui.knowledge.edge_relation.similar', 'Similar');
  if (normalized === 'extends') return text(props, 'ui.knowledge.edge_relation.extends', 'Extends');
  if (normalized === 'next') return text(props, 'ui.knowledge.edge_relation.next', 'Next');
  if (normalized === 'nearby') return text(props, 'ui.knowledge.edge_relation.nearby', 'Nearby');
  return normalized || text(props, 'ui.knowledge.edge_relation.related', 'Related');
}

function enumLabel(props: PluginSurfaceProps, prefix: string, value?: string) {
  const normalized = String(value || '').trim().toLowerCase();
  if (!normalized) return '';
  const keyValue = normalized.replace(/[\s-]+/g, '_');
  return text(props, `${prefix}.${keyValue}`, normalized.replace(/_/g, ' '));
}

function edgePriorityLabel(props: PluginSurfaceProps, priority?: string) {
  return enumLabel(props, 'ui.knowledge.edge_priority', priority);
}

function edgeContextLabel(props: PluginSurfaceProps, context?: string) {
  return enumLabel(props, 'ui.knowledge.edge_context', context);
}

function questionTypeLabel(props: PluginSurfaceProps, questionType?: string) {
  return enumLabel(props, 'ui.knowledge.question_type', questionType);
}

function edgeReason(props: PluginSurfaceProps, edge: KnowledgeEdge, source: string, target: string) {
  const reason = String(edge.reason || '').trim();
  if (reason) return reason;
  const rawTemplate = String(edge.reason_template || '').trim().toLowerCase();
  if (!rawTemplate) return '';
  const template = rawTemplate === 'next'
    ? 'extends'
    : ['nearby', 'similar'].includes(rawTemplate)
      ? 'co_occurs'
      : ['prerequisite', 'procedure_step', 'application', 'confusable', 'extends', 'co_occurs', 'supports', 'analogy'].includes(rawTemplate)
        ? rawTemplate
        : 'related';
  const fallbacks: Record<string, string> = {
    prerequisite: 'Master {source} before studying {target}; it provides the foundation for this topic.',
    procedure_step: 'After learning {source}, practice {target} next to connect the problem-solving steps.',
    application: 'Apply {source} to problems about {target} to practice transferring concepts into use.',
    confusable: '{source} and {target} are easy to confuse; compare their conditions, uses, and boundaries.',
    extends: 'After mastering {source}, continue by extending the idea to {target}.',
    co_occurs: '{source} and {target} are often reviewed together and work well side by side.',
    supports: '{source} supports understanding {target} and can serve as useful explanatory context.',
    analogy: 'Use {source} as an analogy for {target}, while keeping their applicable conditions in mind.',
    related: '{source} is related to {target}; review them together to connect the ideas.',
  };
  return formatText(props, `ui.knowledge.edge_reason.${template}`, fallbacks[template], { source, target });
}

function relationColor(relation?: string) {
  const normalized = String(relation || 'related').trim().toLowerCase();
  if (normalized === 'prerequisite') return '#b7791f';
  if (normalized === 'confusable') return '#c44747';
  if (normalized === 'application') return '#2f7d57';
  if (normalized === 'procedure_step') return '#6d5cc5';
  if (normalized === 'extends' || normalized === 'co_occurs') return '#5f6f82';
  return '#6b8f7b';
}

function edgeGroups(props: PluginSurfaceProps, nodes: KnowledgeNode[], edges: KnowledgeEdge[]) {
  const labels = new Map(nodes.map((node) => [String(node.id || ''), nodeLabel(node)]));
  const groups = new Map<string, { from: string; fromId: string; items: Array<{ relation: string; rawRelation: string; to: string; toId: string; reason: string; priority: string; context: string; confidence: string }> }>();
  edges.slice(0, 80).forEach((edge) => {
    const fromId = String(edge.from || '').trim();
    const toId = String(edge.to || '').trim();
    if (!fromId && !toId) return;
    const key = fromId || '-';
    const group = groups.get(key) || { from: labels.get(key) || key, fromId: key, items: [] };
    const rawRelation = String(edge.relation || 'related').trim().toLowerCase();
    group.items.push({
      relation: relationLabel(props, edge.relation),
      rawRelation,
      to: labels.get(toId) || toId || '-',
      toId,
      reason: edgeReason(props, edge, labels.get(fromId) || fromId || '-', labels.get(toId) || toId || '-'),
      priority: edgePriorityLabel(props, edge.priority),
      context: edgeContextLabel(props, edge.context),
      confidence: Number.isFinite(Number(edge.confidence)) ? `${Math.round(Number(edge.confidence) * 100)}%` : '',
    });
    groups.set(key, group);
  });
  return Array.from(groups.values());
}

function edgeGraph(props: PluginSurfaceProps, nodes: KnowledgeNode[], edges: KnowledgeEdge[]) {
  const labels = new Map(nodes.map((node) => [String(node.id || ''), nodeLabel(node)]));
  const nodesById = new Map(nodes.map((node) => [String(node.id || ''), node]));
  const graphEdges = edgeGroups(props, nodes, edges)
    .slice(0, 12)
    .flatMap((group) => group.items.slice(0, 6).map((item) => ({
      from: String(group.fromId || '').trim(),
      to: String(item.toId || '').trim(),
      relation: String(item.rawRelation || 'related').trim().toLowerCase(),
      label: item.relation,
    })))
    .filter((edge) => edge.from && edge.to)
    .slice(0, 30);
  if (!graphEdges.length) return null;
  const graphIds: string[] = [];
  graphEdges.forEach((edge) => {
    if (!graphIds.includes(edge.from)) graphIds.push(edge.from);
    if (!graphIds.includes(edge.to)) graphIds.push(edge.to);
  });
  const shownIds = graphIds.slice(0, 18);
  const shownIdSet = new Set(shownIds);
  const shownEdges = graphEdges.filter((edge) => shownIdSet.has(edge.from) && shownIdSet.has(edge.to));
  const columnCount = shownIds.length > 10 ? 3 : 2;
  const rowCount = Math.max(1, Math.ceil(shownIds.length / columnCount));
  const width = 920;
  const height = Math.max(240, 88 + rowCount * 82);
  const xStep = width / columnCount;
  const positions = new Map<string, { x: number; y: number }>();
  shownIds.forEach((id, index) => {
    const column = index % columnCount;
    const row = Math.floor(index / columnCount);
    positions.set(id, {
      x: Math.round(xStep * column + xStep / 2),
      y: 58 + row * 82,
    });
  });
  return (
    <div className="knowledge-edge-graph">
      <svg className="knowledge-edge-graph__svg" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={text(props, 'ui.knowledge.edge_graph_label', 'Relationship graph')}>
        <defs>
          <marker id="knowledge-edge-arrow-surface" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor" />
          </marker>
        </defs>
        <g className="knowledge-edge-graph__edges">
          {shownEdges.map((edge, index) => {
            const from = positions.get(edge.from);
            const to = positions.get(edge.to);
            if (!from || !to) return null;
            const dx = Math.max(70, Math.abs(to.x - from.x) * 0.5);
            const controlX1 = from.x + (to.x >= from.x ? dx : -dx);
            const controlX2 = to.x - (to.x >= from.x ? dx : -dx);
            const color = relationColor(edge.relation);
            return (
              <path
                key={`${edge.from}:${edge.to}:${edge.relation}:${index}`}
                className="knowledge-edge-graph__edge"
                data-relation={edge.relation || 'related'}
                d={`M ${from.x} ${from.y} C ${controlX1} ${from.y}, ${controlX2} ${to.y}, ${to.x} ${to.y}`}
                stroke={color}
                color={color}
                markerEnd="url(#knowledge-edge-arrow-surface)"
              >
                <title>{labels.get(edge.from) || edge.from} -&gt; {labels.get(edge.to) || edge.to}: {edge.label}</title>
              </path>
            );
          })}
        </g>
        <g className="knowledge-edge-graph__nodes">
          {shownIds.map((id) => {
            const position = positions.get(id);
            if (!position) return null;
            const label = labels.get(id) || id;
            const boundary = isBoundaryNode(nodesById.get(id));
            return (
              <g key={id} className={`knowledge-edge-graph__node${boundary ? ' knowledge-edge-graph__node--boundary' : ''}`} transform={`translate(${position.x - 88} ${position.y - 29})`}>
                <title>{boundary ? `${label}: ${text(props, 'ui.knowledge.boundary_prerequisite', 'Out-of-scope prerequisite')}` : label}</title>
                <rect width="176" height="58" rx="8" />
                <text x="88" y="24" textAnchor="middle">{label.length > 14 ? `${label.slice(0, 13)}...` : label}</text>
                {boundary ? <text className="knowledge-edge-graph__boundary-label" x="88" y="43" textAnchor="middle">{text(props, 'ui.knowledge.boundary_prerequisite', 'Out-of-scope prerequisite')}</text> : null}
              </g>
            );
          })}
        </g>
      </svg>
    </div>
  );
}

export default function KnowledgeMap(props: PluginSurfaceProps) {
  const [nodes, setNodes] = useState<KnowledgeNode[]>([]);
  const [edges, setEdges] = useState<KnowledgeEdge[]>([]);
  const [selectedNode, setSelectedNode] = useState<KnowledgeNode | null>(null);
  const [defaultStage, setDefaultStage] = useState(readDefaultLearningStage);
  const [selectedStage, setSelectedStage] = useState(() => readDefaultLearningStage() || 'all');
  const [selectedSubject, setSelectedSubject] = useState('all');
  const [selectedChapter, setSelectedChapter] = useState('all');
  const [selectedUnit, setSelectedUnit] = useState('all');
  const [canonicalScope, setCanonicalScope] = useState<PracticeScope | null>(null);
  const [scopeRecoveryFailed, setScopeRecoveryFailed] = useState(false);
  const [scopeBusy, setScopeBusy] = useState(false);
  const [summary, setSummary] = useState<Record<string, number>>({});
  const [relationshipsIncomplete, setRelationshipsIncomplete] = useState(false);
  const [error, setError] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [mapPayload, setMapPayload] = useState<KnowledgeMapPage | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const nodeTriggerRef = useRef<HTMLButtonElement | null>(null);
  const mapLoadRequestRef = useRef(0);
  const mapPayloadRef = useRef<KnowledgeMapPage | null>(null);

  function applyMapPayload(payload: KnowledgeMapPage) {
    mapPayloadRef.current = payload;
    setMapPayload(payload);
    const nextNodes = Array.isArray(payload.nodes) ? payload.nodes : [];
    const nextEdges = Array.isArray(payload.edges) ? payload.edges : [];
    setNodes(nextNodes);
    setEdges(nextEdges);
    setRelationshipsIncomplete(payload.relationships_incomplete === true
      || payload.edge_truncated === true
      || payload.boundary?.truncated === true);
    setSummary(payload.summary || {
      topic_count: Number(payload.scope_total_count || payload.scope_returned_count || nextNodes.length),
      edge_count: nextEdges.length,
    });
  }

  function closeNodeDetail() {
    setSelectedNode(null);
    window.setTimeout(() => {
      if (nodeTriggerRef.current?.isConnected) nodeTriggerRef.current?.focus();
    }, 0);
  }

  useEffect(() => {
    ensureBrandCSS();
    let mounted = true;
    const requestId = mapLoadRequestRef.current += 1;
    setIsLoading(true);
    loadKnowledgeMapPage(props.api, selectedStage, selectedSubject, null)
      .then((payload: any) => {
        if (!mounted || requestId !== mapLoadRequestRef.current) {
          return;
        }
        applyMapPayload(payload);
        setSelectedNode(null);
        setError('');
      })
      .catch((err) => {
        if (!mounted || requestId !== mapLoadRequestRef.current) return;
        setRelationshipsIncomplete(false);
        setError(knowledgeMapErrorMessage(err));
      })
      .finally(() => {
        if (mounted && requestId === mapLoadRequestRef.current) setIsLoading(false);
      });
    return () => {
      mounted = false;
    };
  }, [props.api, selectedStage, selectedSubject]);

  async function loadMoreKnowledgeMap() {
    const current = mapPayloadRef.current;
    if (isLoading || current?.has_more !== true) return;
    const requestId = mapLoadRequestRef.current;
    setIsLoading(true);
    try {
      const payload = await loadKnowledgeMapPage(props.api, selectedStage, selectedSubject, current);
      if (requestId !== mapLoadRequestRef.current) return;
      applyMapPayload(payload);
      setError('');
    } catch (err) {
      if (requestId !== mapLoadRequestRef.current) return;
      setError(knowledgeMapErrorMessage(err));
    } finally {
      if (requestId === mapLoadRequestRef.current) setIsLoading(false);
    }
  }

  useEffect(() => {
    let mounted = true;
    callPlugin(props.api, 'study_get_practice_scope')
      .then((scopePayload: any) => {
        if (!mounted) return;
        const nextScope = scopePayload?.scope && typeof scopePayload.scope === 'object'
          ? scopePayload.scope as PracticeScope
          : null;
        setCanonicalScope(nextScope?.display_path?.length ? nextScope : null);
        setScopeRecoveryFailed(false);
      })
      .catch(() => {
        if (!mounted) return;
        setCanonicalScope(null);
        setScopeRecoveryFailed(true);
      });
    return () => {
      mounted = false;
    };
  }, [props.api]);

  useEffect(() => {
    const syncDefaultStage = (event: StorageEvent) => {
      if (event.key === LEARNING_PROFILE_STORAGE_KEY) {
        setDefaultStage(readDefaultLearningStage());
      }
    };
    window.addEventListener('storage', syncDefaultStage);
    return () => window.removeEventListener('storage', syncDefaultStage);
  }, []);

  useEffect(() => {
    if (selectedNode) closeButtonRef.current?.focus();
  }, [selectedNode]);

  useEffect(() => {
    if (!selectedNode) return undefined;
    const closeActiveDialog = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        closeNodeDetail();
        return;
      }
      if (event.key === 'Tab') {
        const activeDialog = dialogRef.current;
        const focusableElements = Array.from(activeDialog?.querySelectorAll<HTMLElement>('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])') || []);
        const first = focusableElements[0];
        const last = focusableElements[focusableElements.length - 1];
        if (!first || !last) {
          event.preventDefault();
        } else if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener('keydown', closeActiveDialog);
    return () => document.removeEventListener('keydown', closeActiveDialog);
  }, [selectedNode]);

  async function activatePracticeScope(mode: 'explicit_scope' | 'explicit_topic') {
    if (scopeBusy) return;
    const topicNode = mode === 'explicit_topic' ? selectedNode : null;
    const requestedStage = topicNode
      ? String(topicNode.stage || '').trim()
      : selectedStage;
    const requestedModule = topicNode
      ? (requestedStage === 'college'
        ? String(topicNode.course_family || '').trim()
        : String(topicNode.subject || '').trim())
      : selectedSubject;
    if (!requestedStage || requestedStage === 'all' || !requestedModule || requestedModule === 'all') return;
    const scopeNode = topicNode || nodes.find((node) => {
      if (String(node.stage || '').trim() !== requestedStage) return false;
      const module = requestedStage === 'college'
        ? String(node.course_family || '').trim()
        : String(node.subject || '').trim();
      return module === requestedModule;
    });
    if (!scopeNode || (mode === 'explicit_topic' && !topicNode)) return;
    const scope: Record<string, string | number> = {
      schema_version: 1,
      mode,
      stage: requestedStage,
      subject: String(scopeNode.subject || '').trim(),
    };
    if (requestedStage === 'college') scope.course_family = requestedModule;
    const requestedChapter = topicNode
      ? String(topicNode.chapter || '').trim()
      : (selectedChapter === 'all' ? '' : selectedChapter);
    const requestedUnit = topicNode
      ? String(topicNode.unit || '').trim()
      : (selectedUnit === 'all' ? '' : selectedUnit);
    if (requestedChapter) scope.chapter = requestedChapter;
    if (requestedUnit) scope.unit = requestedUnit;
    if (topicNode) scope.topic_id = topicNode.id;

    setScopeBusy(true);
    try {
      const payload = await callPlugin(props.api, 'study_set_practice_scope', { scope }) as {
        scope?: PracticeScope;
        scope_revision?: number;
      };
      const nextScope = payload?.scope && typeof payload.scope === 'object' ? payload.scope : {};
      setCanonicalScope(nextScope);
      setScopeRecoveryFailed(false);
      setError('');
      const rawRevision = payload?.scope_revision ?? nextScope.scope_revision ?? 0;
      const activationRevision = typeof rawRevision === 'number'
        && Number.isSafeInteger(rawRevision)
        && rawRevision >= 0
        ? rawRevision
        : 0;
      postStudySurfaceMessage({
        type: STUDY_SURFACE_MESSAGE_TYPES.openSurface,
        payload: {
          surfaceId: 'study-panel',
          activationRevision,
        },
      }, props.host?.origin);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setScopeBusy(false);
    }
  }

  async function clearPracticeScope() {
    if (scopeBusy) return;
    setScopeBusy(true);
    try {
      await callPlugin(props.api, 'study_clear_practice_scope');
      setCanonicalScope(null);
      setScopeRecoveryFailed(false);
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setScopeBusy(false);
    }
  }

  const inScopeNodes = nodes.filter((node) => !isBoundaryNode(node));
  const hasSubjectFacets = Boolean(mapPayload?.subject_counts && typeof mapPayload.subject_counts === 'object');
  const subjectCounts = new Map<string, number>(Object.entries(mapPayload?.subject_counts || {}).map(
    ([subject, count]) => [subject, Math.max(0, Number(count) || 0)],
  ));
  inScopeNodes.forEach((node) => {
    const subject = nodeSubject(node);
    if (!hasSubjectFacets) {
      subjectCounts.set(subject, (subjectCounts.get(subject) || 0) + 1);
    } else if (!subjectCounts.has(subject)) {
      subjectCounts.set(subject, 0);
    }
  });
  const knownSubjects = KNOWLEDGE_SUBJECT_OPTIONS.filter((subject) => (subjectCounts.get(subject) || 0) > 0);
  const dynamicSubjects = Array.from(subjectCounts.keys()).filter((subject) => subject && !KNOWLEDGE_SUBJECT_OPTIONS.includes(subject));
  const subjects = ['all', ...knownSubjects, ...dynamicSubjects.sort((left, right) => (
    subjectLabel(props, left).localeCompare(subjectLabel(props, right))
  ))];
  if (subjectCounts.has('')) {
    subjects.push('');
  }
  // The server now returns one stage at a time. Keep all selectable stages in
  // the control so a user can request another range even when it is not in the
  // current response.
  const stages = LEARNING_STAGE_OPTIONS.filter((stage) => stage !== 'custom');
  const activeStage = selectedStage !== 'all' && stages.includes(selectedStage)
    ? selectedStage
    : 'all';
  const stageNodes = activeStage === 'all'
    ? inScopeNodes
    : inScopeNodes.filter((node) => String(node.stage || '').trim() === activeStage);
  const moduleField: keyof KnowledgeNode = activeStage === 'college' ? 'course_family' : 'subject';
  const modules = uniqueValues(stageNodes, moduleField);
  const moduleCounts = new Map<string, number>();
  stageNodes.forEach((node) => {
    const module = String(node[moduleField] || '').trim();
    if (module) moduleCounts.set(module, (moduleCounts.get(module) || 0) + 1);
  });
  const selectableModules = activeStage === 'college'
    ? modules
    : subjects.filter((subject) => subject !== 'all' && (subjectCounts.get(subject) || 0) > 0);
  const activeSubject = selectedSubject !== 'all' && (subjectCounts.get(selectedSubject) || 0) > 0
    ? selectedSubject
    : 'all';
  const moduleNodes = activeSubject === 'all'
    ? stageNodes
    : stageNodes.filter((node) => String(node[moduleField] || '').trim() === activeSubject);
  const chapters = uniqueValues(moduleNodes, 'chapter');
  const chapterCounts = new Map(chapters.map((chapter) => [
    chapter,
    moduleNodes.filter((node) => String(node.chapter || '').trim() === chapter).length,
  ]));
  const activeChapter = selectedChapter !== 'all' && chapters.includes(selectedChapter)
    ? selectedChapter
    : 'all';
  const chapterNodes = activeChapter === 'all'
    ? moduleNodes
    : moduleNodes.filter((node) => String(node.chapter || '').trim() === activeChapter);
  const units = uniqueValues(chapterNodes, 'unit');
  const unitCounts = new Map(units.map((unit) => [
    unit,
    chapterNodes.filter((node) => String(node.unit || '').trim() === unit).length,
  ]));
  const activeUnit = selectedUnit !== 'all' && units.includes(selectedUnit)
    ? selectedUnit
    : 'all';
  const scopedNodes = activeUnit === 'all'
    ? chapterNodes
    : chapterNodes.filter((node) => String(node.unit || '').trim() === activeUnit);
  const scopedIds = new Set(scopedNodes.map((node) => String(node.id || '')));
  // The hosted surface loads the complete map, then filters locally. Promote
  // one-hop neighbours to render-only boundary nodes so the selected scope
  // retains its prerequisite context without changing the API payload.
  const boundaryNodes = nodes.flatMap((node) => {
    const nodeId = String(node.id || '');
    if (!nodeId || scopedIds.has(nodeId)) return [];
    const isOneHopAway = edges.some((edge) => (
      (String(edge.from || '') === nodeId && scopedIds.has(String(edge.to || '')))
      || (String(edge.to || '') === nodeId && scopedIds.has(String(edge.from || '')))
    ));
    return isOneHopAway ? [{ ...node, boundary: true, in_scope: false }] : [];
  });
  const visibleNodes = [...scopedNodes, ...boundaryNodes];
  const visibleIds = new Set(visibleNodes.map((node) => String(node.id || '')));
  const visibleNodeById = new Map(visibleNodes.map((node) => [String(node.id || ''), node]));
  const visibleEdges = edges.filter((edge) => (
    visibleIds.has(String(edge.from || '')) && visibleIds.has(String(edge.to || ''))
  ));
  const currentNode = selectedNode && visibleIds.has(String(selectedNode.id || ''))
    ? selectedNode
    : null;
  const activeSubjectLabel = activeSubject === 'all'
    ? text(props, 'ui.knowledge.module_all', 'All course modules')
    : activeStage === 'college'
      ? valueLabel(activeSubject)
      : subjectLabel(props, activeSubject);
  const loadingSubjectText = text(props, 'ui.knowledge.loading_subject', 'Loading {subject} knowledge map...')
    .replace('{subject}', activeSubjectLabel);
  const emptyDetailItem = text(props, 'ui.knowledge.node_detail.empty', 'Keep studying this topic to unlock more graph context.');
  const weakTopicCount = scopedNodes.filter((node) => nodeIsWeakTopic(node)).length;

  return (
    <div className="study-panel surface-shell">
      <header className="study-panel__header">
        <div>
          <h1>{text(props, 'ui.surface.knowledge_map', 'Knowledge Map')}</h1>
          <span>{summary.topic_count || inScopeNodes.length} / {weakTopicCount}</span>
        </div>
      </header>
      {error ? <pre>{error}</pre> : null}
      {relationshipsIncomplete ? <p className="knowledge-map-incomplete" role="status">
        {text(props, 'ui.knowledge.relationships_incomplete', 'Some relationships could not be loaded completely.')}
      </p> : null}
      <section className="study-panel__state">
        <div>
          <span>{text(props, 'ui.label.topics', 'Topics')}</span>
          <strong>{scopedNodes.length} / {summary.topic_count || inScopeNodes.length}</strong>
        </div>
        <div>
          <span>{text(props, 'ui.label.edges', 'Edges')}</span>
          <strong>{visibleEdges.length} / {summary.edge_count || edges.length}</strong>
        </div>
        <div>
          <span>{text(props, 'ui.label.weak_topics', 'Weak Topics')}</span>
          <strong>{weakTopicCount}</strong>
        </div>
        <div>
          <span>{text(props, 'ui.knowledge.module_label', 'Course module')}</span>
          <strong>{activeSubjectLabel}</strong>
        </div>
        {boundaryNodes.length ? <div>
          <span>{text(props, 'ui.knowledge.boundary_prerequisites', 'Out-of-scope prerequisites')}</span>
          <strong>{boundaryNodes.length}</strong>
        </div> : null}
      </section>
      {canonicalScope?.display_path?.length || scopeRecoveryFailed ? (
        <section className="study-panel__state" aria-live="polite">
          {canonicalScope?.display_path?.length ? <div>
            <span>{text(props, 'ui.practice.scope_label', 'Practice scope')}</span>
            <strong>{canonicalScope.display_path.join(' / ')}</strong>
          </div> : null}
          <button type="button" disabled={scopeBusy} onClick={() => void clearPracticeScope()}>
            {text(props, 'ui.button.clear_practice_scope', 'Clear scope')}
          </button>
        </section>
      ) : null}
      <section className="knowledge-stage-selector">
        <span>{text(props, 'ui.knowledge.stage_label', 'Learning stage')}</span>
        <div className="knowledge-stage-selector__actions">
          {['all', ...stages].map((stage) => {
            const label = stage === 'all'
              ? text(props, 'ui.knowledge.scope_all', 'All stages')
              : stageLabel(props, stage);
            const count = stage === 'all'
              ? inScopeNodes.length
              : inScopeNodes.filter((node) => String(node.stage || '').trim() === stage).length;
            return (
              <button
                key={stage || 'uncategorized'}
                type="button"
                className="knowledge-stage-option"
                data-stage={stage || 'uncategorized'}
                aria-pressed={stage === activeStage ? 'true' : 'false'}
                onClick={() => {
                  setSelectedStage(stage);
                  setSelectedSubject('all');
                  setSelectedChapter('all');
                  setSelectedUnit('all');
                  setSelectedNode(null);
                }}
              >
                {count ? `${label} ${count}` : label}
              </button>
            );
          })}
        </div>
        <div className="knowledge-stage-selector__actions knowledge-stage-selector__quick-actions">
          <button
            type="button"
            disabled={activeStage === 'all' || activeStage === defaultStage}
            onClick={() => {
              const nextDefaultStage = writeDefaultLearningStage(activeStage);
              if (!nextDefaultStage) return;
              setDefaultStage(nextDefaultStage);
              setSelectedStage(nextDefaultStage);
              setSelectedSubject('all');
              setSelectedChapter('all');
              setSelectedUnit('all');
              setSelectedNode(null);
            }}
          >
            {text(props, 'ui.knowledge.set_default_stage', 'Set as default stage')}
          </button>
          <button
            type="button"
            disabled={!defaultStage || activeStage === defaultStage}
            onClick={() => {
              if (!defaultStage) return;
              setSelectedStage(defaultStage);
              setSelectedSubject('all');
              setSelectedChapter('all');
              setSelectedUnit('all');
              setSelectedNode(null);
            }}
          >
            {text(props, 'ui.knowledge.return_default_stage', 'Return to default stage')}
          </button>
        </div>
      </section>
      {activeStage !== 'all' ? (
        <section className="knowledge-stage-selector knowledge-subject-selector">
          <span>{text(props, 'ui.knowledge.module_label', 'Course module')}</span>
          <div className="knowledge-stage-selector__actions">
            {selectableModules.map((module) => {
              const label = activeStage === 'college' ? valueLabel(module) : subjectLabel(props, module);
              const count = activeStage === 'college'
                ? (moduleCounts.get(module) || 0)
                : (subjectCounts.get(module) || 0);
              return (
                <button
                  key={module}
                  type="button"
                  className="knowledge-stage-option"
                  data-module={module}
                  aria-pressed={module === activeSubject ? 'true' : 'false'}
                  onClick={() => {
                    setSelectedSubject(module);
                    setSelectedChapter('all');
                    setSelectedUnit('all');
                    setSelectedNode(null);
                  }}
                >
                  {`${label} ${count}`}
                </button>
              );
            })}
          </div>
        </section>
      ) : null}
      {activeSubject !== 'all' ? (
        <section className="knowledge-stage-selector knowledge-chapter-selector">
          <span>{text(props, 'ui.knowledge.chapter_label', 'Chapter')}</span>
          <div className="knowledge-stage-selector__actions">
            <button
              type="button"
              className="knowledge-stage-option"
              aria-pressed={activeChapter === 'all' ? 'true' : 'false'}
              onClick={() => {
                setSelectedChapter('all');
                setSelectedUnit('all');
                setSelectedNode(null);
              }}
            >
              {text(props, 'ui.knowledge.chapter_all', 'All chapters')}
            </button>
            {chapters.map((chapter) => (
              <button
                key={chapter}
                type="button"
                className="knowledge-stage-option"
                data-chapter={chapter}
                aria-pressed={chapter === activeChapter ? 'true' : 'false'}
                onClick={() => {
                  setSelectedChapter(chapter);
                  setSelectedUnit('all');
                  setSelectedNode(null);
                }}
              >
                {`${chapter} ${chapterCounts.get(chapter) || 0}`}
              </button>
            ))}
          </div>
        </section>
      ) : null}
      {activeChapter !== 'all' ? (
        <section className="knowledge-stage-selector knowledge-unit-selector">
          <span>{text(props, 'ui.knowledge.unit_label', 'Unit')}</span>
          <div className="knowledge-stage-selector__actions">
            <button
              type="button"
              className="knowledge-stage-option"
              aria-pressed={activeUnit === 'all' ? 'true' : 'false'}
              onClick={() => {
                setSelectedUnit('all');
                setSelectedNode(null);
              }}
            >
              {text(props, 'ui.knowledge.unit_all', 'All units')}
            </button>
            {units.map((unit) => (
              <button
                key={unit}
                type="button"
                className="knowledge-stage-option"
                data-unit={unit}
                aria-pressed={unit === activeUnit ? 'true' : 'false'}
                onClick={() => {
                  setSelectedUnit(unit);
                  setSelectedNode(null);
                }}
              >
                {`${unit} ${unitCounts.get(unit) || 0}`}
              </button>
            ))}
          </div>
        </section>
      ) : null}
      {isLoading ? (
        <pre>{loadingSubjectText}</pre>
      ) : null}
      {mapPayload?.has_more === true ? (
        <div className="study-panel__actions" aria-live="polite">
          <button type="button" disabled={isLoading} onClick={() => void loadMoreKnowledgeMap()}>
            {text(props, 'ui.knowledge.load_more', 'Load more topics')}
          </button>
        </div>
      ) : null}
      {!isLoading ? <section className="knowledge-topic-section knowledge-topic-section--scope">
        <header className="knowledge-topic-section__header">
          <strong>{text(props, 'ui.knowledge.scope_topics', 'Topics in current scope')}</strong>
          <span className="knowledge-topic-section__count">{scopedNodes.length}</span>
        </header>
        <p className="knowledge-topic-section__hint">
          {text(props, 'ui.knowledge.scope_topics_description', 'Practice uses only the topics in this section.')}
        </p>
        <div className="study-panel__actions">
        {scopedNodes.slice(0, 60).map((node) => {
          const masteryText = nodeMasteryText(props, node);
          return (
            <button
              key={node.id}
              type="button"
              className="knowledge-node"
              data-mastery={nodeMasteryLevel(node)}
              aria-pressed={currentNode?.id === node.id ? 'true' : 'false'}
              onClick={(event: any) => {
                nodeTriggerRef.current = event.currentTarget;
                setSelectedNode(node);
              }}
            >
              <span className="knowledge-node__label">{node.label}{masteryText}</span>
            </button>
          );
        })}
        {scopedNodes.length > 60 ? (
          <span className="knowledge-edge-more">+ {scopedNodes.length - 60} {text(props, 'ui.knowledge.edge_more_suffix', 'more')}</span>
        ) : null}
        </div>
      </section> : null}
      {!isLoading && boundaryNodes.length ? (
        <details className="knowledge-related-section">
          <summary className="knowledge-related-section__summary">
            <strong>{text(props, 'ui.knowledge.related_topics', 'Related topics')}</strong>
            <span className="knowledge-topic-section__count">{boundaryNodes.length}</span>
          </summary>
          <p className="knowledge-topic-section__hint">
            {text(
              props,
              'ui.knowledge.related_topics_description',
              'These topics explain prerequisites, applications, confusable ideas, and extensions. They are not included in the current practice scope.',
            )}
          </p>
          <div className="knowledge-related-list">
            {boundaryNodes.slice(0, 24).map((node) => {
              const nodeId = String(node.id || '');
              const connections = visibleEdges.filter((edge) => (
                (String(edge.from || '') === nodeId && scopedIds.has(String(edge.to || '')))
                || (String(edge.to || '') === nodeId && scopedIds.has(String(edge.from || '')))
              ));
              const primary = connections[0];
              const rawRelation = String(primary?.relation || 'related').trim().toLowerCase();
              const source = primary ? nodeLabel(visibleNodeById.get(String(primary.from || '')) || { id: primary.from }) : '';
              const target = primary ? nodeLabel(visibleNodeById.get(String(primary.to || '')) || { id: primary.to }) : '';
              const reason = primary ? edgeReason(props, primary, source, target) : '';
              const path = [
                stageLabel(props, String(node.stage || '')),
                subjectLabel(props, String(node.subject || '')),
                node.chapter,
                node.unit,
              ].filter(Boolean).join(' / ');
              return (
                <button
                  key={node.id}
                  type="button"
                  className="knowledge-related-card"
                  data-boundary="true"
                  data-relation={rawRelation}
                  aria-label={`${nodeLabel(node)}: ${relationLabel(props, primary?.relation)}`}
                  onClick={(event: any) => {
                    nodeTriggerRef.current = event.currentTarget;
                    setSelectedNode(node);
                  }}
                >
                  <span className="knowledge-related-card__top">
                    <small className="knowledge-related-card__path">{path}</small>
                    <span className="knowledge-related-card__relation">{relationLabel(props, primary?.relation)}</span>
                  </span>
                  <strong className="knowledge-related-card__title">{nodeLabel(node)}</strong>
                  {primary ? <span className="knowledge-related-card__direction">{source} → {target}</span> : null}
                  {reason ? <small className="knowledge-related-card__reason">{reason}</small> : null}
                  {connections.length > 1 ? (
                    <small className="knowledge-related-card__more">
                      {formatText(
                        props,
                        'ui.knowledge.related_more_connections',
                        '+ {count} more connections',
                        { count: connections.length - 1 },
                      )}
                    </small>
                  ) : null}
                </button>
              );
            })}
            {boundaryNodes.length > 24 ? (
              <span className="knowledge-edge-more">+ {boundaryNodes.length - 24} {text(props, 'ui.knowledge.edge_more_suffix', 'more')}</span>
            ) : null}
          </div>
        </details>
      ) : null}
      <div className="study-panel__reply-label">{text(props, 'ui.knowledge.edge_section', 'Relationships')}</div>
      {!isLoading && visibleEdges.length ? edgeGraph(props, visibleNodes, visibleEdges) : null}
      <div className="knowledge-edge-list">
        {!isLoading ? edgeGroups(props, visibleNodes, visibleEdges).slice(0, 12).map((group) => (
          <article key={group.fromId} className="knowledge-edge-card">
            <h3>{group.from}</h3>
            <div className="knowledge-edge-card__items">
              {group.items.slice(0, 6).map((item, index) => (
                <div
                  key={`${item.rawRelation}:${item.to}:${index}`}
                  className="knowledge-edge-row"
                  data-relation={item.rawRelation || 'related'}
                  data-priority={item.priority || 'optional'}
                  data-context={item.context || 'review'}
                >
                  <span className="knowledge-edge-row__relation">{item.relation}</span>
                  <span className="knowledge-edge-row__target">
                    {item.to}
                    {item.reason ? <small className="knowledge-edge-row__reason">{item.reason}</small> : null}
                    {item.priority || item.context || item.confidence ? (
                      <small className="knowledge-edge-row__meta">
                        {[item.priority, item.context, item.confidence].filter(Boolean).join(' / ')}
                      </small>
                    ) : null}
                  </span>
                </div>
              ))}
              {group.items.length > 6 ? (
                <span className="knowledge-edge-more">+ {group.items.length - 6} {text(props, 'ui.knowledge.edge_more_suffix', 'more')}</span>
              ) : null}
            </div>
          </article>
        )) : null}
        {!isLoading && !visibleEdges.length ? (
          <pre>{summary.topic_count || nodes.length
            ? text(props, 'ui.knowledge.edge_empty', 'No relationships to show yet.')
            : text(props, 'ui.settings.knowledge.empty_summary', 'Knowledge map has no loaded topics yet.')}</pre>
        ) : null}
      </div>
      {!isLoading && currentNode ? (
        <div
          ref={dialogRef}
          className="knowledge-node-detail-dialog"
          role="dialog"
          aria-modal="true"
          aria-label={nodeLabel(currentNode)}
          onClick={(event: any) => {
            if (event.target === event.currentTarget) closeNodeDetail();
          }}
        >
          <div className="knowledge-node-detail-dialog__panel">
            <header className="knowledge-node-detail-dialog__header">
              <strong>{nodeLabel(currentNode)}</strong>
              <button ref={closeButtonRef} type="button" className="button button-secondary knowledge-node-detail-dialog__close" onClick={closeNodeDetail}>
                {text(props, 'ui.button.close', 'Close')}
              </button>
            </header>
            <article className="knowledge-node-detail">
              <h3>{nodeLabel(currentNode)}</h3>
              <p className="knowledge-node-detail__meta">
                {[currentNode.subject ? subjectLabel(props, currentNode.subject) : '', currentNode.chapter, currentNode.unit].filter(Boolean).join(' / ')}
              </p>
              {isBoundaryNode(currentNode) ? <p className="knowledge-node-detail__boundary">{text(props, 'ui.knowledge.boundary_description', 'This prerequisite is shown for context and is outside the selected graph range.')}</p> : null}
              <section className="knowledge-node-detail__section">
                <h4>{text(props, 'ui.knowledge.node_detail.why', 'Why connected')}</h4>
                <ul className="knowledge-node-detail__list">
                  {visibleEdges
                    .filter((edge) => edge.from === currentNode.id || edge.to === currentNode.id)
                    .slice(0, 4)
                    .map((edge, index) => {
                      const otherId = edge.from === currentNode.id ? edge.to : edge.from;
                      const otherNode = visibleNodes.find((node) => node.id === otherId);
                      return (
                        <li key={`${edge.from}:${edge.to}:${index}`}>
                          {relationLabel(props, edge.relation)}: {nodeLabel(otherNode || { id: otherId })}{(() => {
                            const source = nodeLabel(visibleNodes.find((node) => node.id === edge.from) || { id: edge.from });
                            const target = nodeLabel(visibleNodes.find((node) => node.id === edge.to) || { id: edge.to });
                            const reason = edgeReason(props, edge, source, target);
                            return reason ? ` - ${reason}` : '';
                          })()}
                        </li>
                      );
                    })}
                  {!visibleEdges.some((edge) => edge.from === currentNode.id || edge.to === currentNode.id) ? <li>{emptyDetailItem}</li> : null}
                </ul>
              </section>
              <section className="knowledge-node-detail__section">
                <h4>{text(props, 'ui.knowledge.node_detail.next', 'Recommended next step')}</h4>
                <ul className="knowledge-node-detail__list">
                  {visibleEdges
                    .filter((edge) => edge.from === currentNode.id && ['application', 'procedure_step', 'extends'].includes(String(edge.relation || '').trim().toLowerCase()))
                    .slice(0, 3)
                    .map((edge, index) => {
                      const target = visibleNodes.find((node) => node.id === edge.to);
                      return <li key={`${edge.to}:${index}`}>{relationLabel(props, edge.relation)}: {nodeLabel(target || { id: edge.to })}</li>;
                    })}
                  {!visibleEdges.some((edge) => edge.from === currentNode.id && ['application', 'procedure_step', 'extends'].includes(String(edge.relation || '').trim().toLowerCase())) ? <li>{emptyDetailItem}</li> : null}
                </ul>
              </section>
              <section className="knowledge-node-detail__section">
                <h4>{text(props, 'ui.knowledge.node_detail.practice', 'Practice type')}</h4>
                <ul className="knowledge-node-detail__list">
                  {(currentNode.question_types || []).slice(0, 3).map((item) => <li key={item}>{questionTypeLabel(props, item)}</li>)}
                  {!(currentNode.question_types || []).length ? <li>{emptyDetailItem}</li> : null}
                </ul>
              </section>
              <section className="knowledge-node-detail__section">
                <h4>{text(props, 'ui.knowledge.node_detail.misconceptions', 'Common misconceptions')}</h4>
                <ul className="knowledge-node-detail__list">
                  {(currentNode.typical_misconceptions || []).slice(0, 3).map((item) => <li key={item}>{item}</li>)}
                  {!(currentNode.typical_misconceptions || []).length ? <li>{emptyDetailItem}</li> : null}
                </ul>
              </section>
              <div className="study-panel__actions">
                <button
                  type="button"
                  disabled={scopeBusy}
                  onClick={() => void activatePracticeScope('explicit_topic')}
                >
                  {text(props, 'ui.knowledge.practice_topic', 'Practice this topic')}
                </button>
                <button
                  type="button"
                  disabled={scopeBusy || activeStage === 'all' || activeSubject === 'all'}
                  onClick={() => void activatePracticeScope('explicit_scope')}
                >
                  {text(props, 'ui.knowledge.practice_current_scope', 'Start practice in current scope')}
                </button>
              </div>
            </article>
          </div>
        </div>
      ) : null}
    </div>
  );
}
