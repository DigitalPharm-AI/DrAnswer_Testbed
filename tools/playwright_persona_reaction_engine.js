const SUCCESS_ACTIONS = new Set(["take", "reply"]);
const TAKE_ACTION = "take";
const REPLY_ACTION = "reply";
const OPT_OUT_ACTIONS = new Set(["suppress"]);

function decideReaction(persona, notificationContext, simulationContext = {}) {
  const toneKey = notificationContext?.tone_policy?.tone_key || notificationContext?.tone_key || "";
  const rules = persona?.rules || {};
  const action = rules[toneKey] || "ignore";
  const success = SUCCESS_ACTIONS.has(action);
  return {
    persona_id: persona.id,
    action,
    success,
    tone_key: toneKey,
    policy_variant: notificationContext?.tone_policy?.policy_variant || notificationContext?.policy_variant || "",
    simulation_day: simulationContext.day || notificationContext.simulation_day || 0,
  };
}

function summarizeReactions(reactions) {
  const byTone = {};
  for (const reaction of reactions) {
    const tone = reaction.tone_key || "unknown";
    if (!byTone[tone]) {
      byTone[tone] = { total: 0, success: 0, actions: {} };
    }
    byTone[tone].total += 1;
    if (reaction.success) {
      byTone[tone].success += 1;
    }
    byTone[tone].actions[reaction.action] = (byTone[tone].actions[reaction.action] || 0) + 1;
  }
  for (const tone of Object.keys(byTone)) {
    byTone[tone].success_rate = byTone[tone].total ? byTone[tone].success / byTone[tone].total : 0;
  }
  return byTone;
}

function firstActionDay(reactions, action) {
  const match = reactions.find((reaction) => reaction.action === action);
  return match ? match.simulation_day : null;
}

function firstToneActionDay(reactions, toneKey, action) {
  const match = reactions.find((reaction) => reaction.tone_key === toneKey && reaction.action === action);
  return match ? match.simulation_day : null;
}

function toneActionCount(toneSummary, action) {
  return toneSummary && toneSummary.actions ? toneSummary.actions[action] || 0 : 0;
}

function chooseBestTone(byTone, action) {
  const candidates = Object.entries(byTone)
    .map(([toneKey, summary]) => ({
      toneKey,
      count: toneActionCount(summary, action),
      total: summary.total,
      successRate: summary.success_rate,
      summary,
    }))
    .filter((candidate) => candidate.count > 0);
  candidates.sort((left, right) => {
    if (right.count !== left.count) return right.count - left.count;
    if (right.successRate !== left.successRate) return right.successRate - left.successRate;
    return left.toneKey.localeCompare(right.toneKey);
  });
  return candidates[0] || null;
}

function avoidToneKeys(byTone) {
  return Object.entries(byTone)
    .filter(([, summary]) => {
      if (toneActionCount(summary, "suppress") > 0) return true;
      if (summary.total >= 2 && summary.success === 0) return true;
      return false;
    })
    .map(([toneKey]) => toneKey);
}

function analyzePersonaOutcome(persona, reactions) {
  const byTone = summarizeReactions(reactions);
  const bestTakeTone = chooseBestTone(byTone, TAKE_ACTION);
  const bestReplyTone = chooseBestTone(byTone, REPLY_ACTION);
  const optOutCount = reactions.filter((reaction) => OPT_OUT_ACTIONS.has(reaction.action)).length;
  const takeCount = reactions.filter((reaction) => reaction.action === TAKE_ACTION).length;
  const replyCount = reactions.filter((reaction) => reaction.action === REPLY_ACTION).length;
  const avoidTones = avoidToneKeys(byTone);

  let recommendedNextTone = "";
  let strategy = "";
  let rationale = "";
  if (optOutCount > 0 && bestReplyTone) {
    recommendedNextTone = bestReplyTone.toneKey;
    strategy = "support_first";
    rationale = `${bestReplyTone.toneKey} produced replies while opt-out appeared in another tone.`;
  } else if (bestTakeTone) {
    recommendedNextTone = bestTakeTone.toneKey;
    strategy = "direct_adherence";
    rationale = `${bestTakeTone.toneKey} produced the strongest take response.`;
  } else if (bestReplyTone) {
    recommendedNextTone = bestReplyTone.toneKey;
    strategy = "conversation_first";
    rationale = `${bestReplyTone.toneKey} produced engagement without take conversion.`;
  } else {
    recommendedNextTone = "practical";
    strategy = "explore_practical";
    rationale = "No successful reaction was observed, so use a short practical prompt next.";
  }

  return {
    persona_id: persona.id,
    persona_label: persona.label || persona.id,
    recommended_next_tone: recommendedNextTone,
    strategy,
    rationale,
    avoid_tones: avoidTones,
    first_take_day: firstActionDay(reactions, TAKE_ACTION),
    first_reply_day: firstActionDay(reactions, REPLY_ACTION),
    take_count: takeCount,
    reply_count: replyCount,
    opt_out_count: optOutCount,
    by_tone: byTone,
    first_recommended_tone_take_day: recommendedNextTone ? firstToneActionDay(reactions, recommendedNextTone, TAKE_ACTION) : null,
    first_recommended_tone_reply_day: recommendedNextTone ? firstToneActionDay(reactions, recommendedNextTone, REPLY_ACTION) : null,
  };
}

module.exports = {
  analyzePersonaOutcome,
  decideReaction,
  summarizeReactions,
};
