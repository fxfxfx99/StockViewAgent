const LEVELS = new Set(["beginner", "experienced"]);
export const LEARNING_STEP_IDS = {
  beginner: ["money", "products", "reading", "observation"],
  experienced: ["reports", "evidence", "review"],
};

export function learningLevel(value) {
  return LEVELS.has(value) ? value : null;
}

export function learningStorageKey(userId) {
  return `sva_learning_v1:${userId ?? "guest"}`;
}

export function readLearningPreferences(storage, key) {
  try {
    const value = JSON.parse(storage.getItem(key) || "null");
    return {
      level: learningLevel(value?.level) || "beginner",
      completed: Array.isArray(value?.completed)
        ? [...new Set(value.completed.filter((id) => Object.values(LEARNING_STEP_IDS).flat().includes(id)))]
        : [],
    };
  } catch {
    return { level: "beginner", completed: [] };
  }
}
