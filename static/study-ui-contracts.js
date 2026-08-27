/* GENERATED FROM surfaces/study_ui_contracts.ts. DO NOT EDIT BY HAND. */
(function attachStudyUiContracts(global) {
  var exports = {};
"use strict";
/**
 * Pure study-surface contracts shared with the legacy static workspace.
 *
 * Keep this module free of DOM and plugin-host imports.  Its checked-in ES5
 * counterpart is static/study-ui-contracts.js, which is loaded before the
 * legacy workspace scripts.
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.knowledgeMapErrorCode = knowledgeMapErrorCode;
exports.knowledgeMapErrorMessage = knowledgeMapErrorMessage;
exports.isKnowledgeMapCursorStale = isKnowledgeMapCursorStale;
exports.isScopeKnowledgeNode = isScopeKnowledgeNode;
exports.mergeKnowledgeMapPage = mergeKnowledgeMapPage;
function knowledgeMapErrorCode(error) {
    if (error && typeof error === 'object' && 'code' in error) {
        return String(error.code || '').trim().toUpperCase();
    }
    return '';
}
function knowledgeMapErrorMessage(error) {
    if (error instanceof Error)
        return error.message;
    if (error && typeof error === 'object' && 'message' in error) {
        return String(error.message || '');
    }
    return String(error || '');
}
function isKnowledgeMapCursorStale(error) {
    return knowledgeMapErrorCode(error) === 'KNOWLEDGE_MAP_CURSOR_STALE'
        || /knowledge_map_cursor_stale/i.test(knowledgeMapErrorMessage(error));
}
function isScopeKnowledgeNode(node) {
    return node?.boundary !== true && node?.in_scope !== false;
}
function uniqueNodes(nodes) {
    const byId = new Map();
    nodes.forEach((node) => {
        const id = String(node?.id || '').trim();
        if (!id)
            return;
        const current = byId.get(id);
        // An in-scope node supersedes the temporary one-hop boundary copy.
        if (!current || (isScopeKnowledgeNode(node) && !isScopeKnowledgeNode(current))) {
            byId.set(id, node);
        }
    });
    return Array.from(byId.values());
}
function uniqueEdges(edges) {
    const byKey = new Map();
    edges.forEach((edge) => {
        const from = String(edge?.from || '').trim();
        const to = String(edge?.to || '').trim();
        const relation = String(edge?.relation || '').trim();
        if (from && to)
            byKey.set(`${from}\u0000${to}\u0000${relation}`, edge);
    });
    return Array.from(byKey.values());
}
function uniqueItems(items, key) {
    const byKey = new Map();
    const unkeyed = [];
    items.forEach((item) => {
        if (!item || typeof item !== 'object') {
            unkeyed.push(item);
            return;
        }
        const value = String(item[key] || '').trim();
        if (!value) {
            unkeyed.push(item);
            return;
        }
        byKey.set(value, item);
    });
    return [...byKey.values(), ...unkeyed];
}
/** Merge one V2 page without deciding when another page should be requested. */
function mergeKnowledgeMapPage(current, page) {
    const base = current || page;
    const nodes = uniqueNodes([
        ...(Array.isArray(current?.nodes) ? current.nodes : []),
        ...(Array.isArray(page?.nodes) ? page.nodes : []),
    ]);
    const edges = uniqueEdges([
        ...(Array.isArray(current?.edges) ? current.edges : []),
        ...(Array.isArray(page?.edges) ? page.edges : []),
    ]);
    const masteryOverview = uniqueItems([
        ...(Array.isArray(current?.mastery_overview) ? current.mastery_overview : []),
        ...(Array.isArray(page?.mastery_overview) ? page.mastery_overview : []),
    ], 'topic_id');
    const weakTopics = uniqueItems([
        ...(Array.isArray(current?.weak_topics) ? current.weak_topics : []),
        ...(Array.isArray(page?.weak_topics) ? page.weak_topics : []),
    ], 'topic_id');
    const wrongQuestions = uniqueItems([
        ...(Array.isArray(current?.wrong_questions) ? current.wrong_questions : []),
        ...(Array.isArray(page?.wrong_questions) ? page.wrong_questions : []),
    ], 'id');
    const scopeReturnedCount = nodes.filter(isScopeKnowledgeNode).length;
    const boundaryReturnedCount = nodes.length - scopeReturnedCount;
    const edgeTruncated = current?.edge_truncated === true || current?.edges_truncated === true
        || page?.edge_truncated === true || page?.edges_truncated === true;
    const boundaryTruncated = current?.boundary?.truncated === true || page?.boundary?.truncated === true;
    const omittedEdgeCount = Math.max(0, Number(current?.omitted_edge_count || 0) || 0)
        + Math.max(0, Number(page?.omitted_edge_count || 0) || 0);
    const baseSummary = base?.summary && typeof base.summary === 'object' ? base.summary : {};
    return {
        ...base,
        catalog_revision: page?.catalog_revision || current?.catalog_revision || undefined,
        nodes,
        edges,
        mastery_overview: masteryOverview,
        weak_topics: weakTopics,
        wrong_questions: wrongQuestions,
        scope_returned_count: scopeReturnedCount,
        boundary: {
            ...(base?.boundary && typeof base.boundary === 'object' ? base.boundary : {}),
            ...(page?.boundary && typeof page.boundary === 'object' ? page.boundary : {}),
            returned_count: boundaryReturnedCount,
            truncated: boundaryTruncated,
        },
        summary: {
            ...baseSummary,
            topic_count: scopeReturnedCount,
            scope_topic_count: scopeReturnedCount,
            boundary_node_count: boundaryReturnedCount,
            edge_count: edges.length,
            weak_topic_count: weakTopics.length,
            wrong_question_count: wrongQuestions.length,
        },
        edge_truncated: edgeTruncated,
        omitted_edge_count: omittedEdgeCount,
        has_more: page?.has_more === true,
        next_cursor: String(page?.next_cursor || '').trim(),
        relationships_incomplete: edgeTruncated || boundaryTruncated,
    };
}
  global.StudyCompanionUiContracts = Object.freeze({
    errorCode: exports.knowledgeMapErrorCode,
    errorMessage: exports.knowledgeMapErrorMessage,
    isCursorStale: exports.isKnowledgeMapCursorStale,
    isScopeNode: exports.isScopeKnowledgeNode,
    mergeKnowledgeMapPage: exports.mergeKnowledgeMapPage,
  });
})(window);
