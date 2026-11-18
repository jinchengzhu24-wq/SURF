using System;
using System.Collections;
using System.Collections.Generic;
using UnityEngine;

[DefaultExecutionOrder(-100)]
public partial class LevelGenerator : MonoBehaviour
{
    [Header("References")]
    public LevelData levelData;
    public LevelLoader levelLoader;
    public LevelSolver levelSolver;
    public LevelGenerationRules rules;

    [Header("Runtime")]
    public bool generateOnAwake = true;
    public bool logGenerationResult = true;

    [Header("Last Result")]
    public int lastAttempts;
    public int lastReversePulls;
    public int lastSearchedStates;
    public int lastSolutionSteps;
    public int lastPushes;

    [Header("Repeat Prevention")]
    public bool rejectRecentlyGeneratedLayouts = true;
    public int recentGeneratedLayoutHistory = 6;

    private const char Ground = '.';
    private const char Wall = '#';
    private const char Water = '@';
    private const char Empty = ' ';
    private const char Player = 'p';
    private const char Box = 's';
    private const char Target = 't';
    private const int LLMTopCandidatePoolSize = 3;

    private const string DefaultArchetype = "open_workshop";
    private const string DefaultTargetLayout = "split_pair";
    private const string DefaultObstacleStyle = "central_baffle";
    private const string DefaultWaterStyle = "side_pool";
    private enum CandidateFailureReason
    {
        None,
        BaseGrid,
        WallObstacles,
        WaterAreas,
        Targets,
        Player,
        ReversePulls,
        TileRules,
        BuildRows
    }

    private sealed class LevelQualityReport
    {
        public int score;
        public int solutionSteps;
        public int pushes;
        public int reversePulls;
        public int searchedStates;
        public int waterTiles;
        public int waterAreas;
        public int surroundedWallCount;
        public int obstacleInfluence;
        public int boxInteractionScore;
        public int targetDistance;
        public int structureSimilarity;

        public string Summary()
        {
            return "score=" + score
                + ", solutionSteps=" + solutionSteps
                + ", pushes=" + pushes
                + ", reversePulls=" + reversePulls
                + ", searchedStates=" + searchedStates
                + ", waterTiles=" + waterTiles
                + ", waterAreas=" + waterAreas
                + ", surroundedWalls=" + surroundedWallCount
                + ", obstacleInfluence=" + obstacleInfluence
                + ", boxInteractionScore=" + boxInteractionScore
                + ", targetDistance=" + targetDistance
                + ", structureSimilarity=" + structureSimilarity;
        }
    }

    private sealed class GeneratedCandidate
    {
        public readonly string[] rows;
        public readonly int attempt;
        public readonly LevelQualityReport quality;
        public readonly CorridorValidationResult corridorValidation;

        public GeneratedCandidate(
            string[] rows,
            int attempt,
            LevelQualityReport quality,
            CorridorValidationResult corridorValidation)
        {
            this.rows = rows;
            this.attempt = attempt;
            this.quality = quality;
            this.corridorValidation = corridorValidation;
        }
    }

    private static readonly Queue<string> recentFullLevelSignatures = new Queue<string>();
    private static readonly HashSet<string> recentFullLevelLookup = new HashSet<string>();
    private static readonly Queue<string> recentStructureSignatures = new Queue<string>();
    private static readonly HashSet<string> recentStructureLookup = new HashSet<string>();
    private static int runtimeSeedCounter;
    private static int algorithmTemplateRoundCounter;
    private static int algorithmTemplateCursor;
    private static int[] algorithmTemplateOrder;
    private static bool algorithmTemplateOrderNeedsShuffle = true;

    private static readonly Vector2Int[] directions =
    {
        new Vector2Int(0, -1),
        new Vector2Int(0, 1),
        new Vector2Int(-1, 0),
        new Vector2Int(1, 0)
    };

    private readonly List<Vector2Int> boxes = new List<Vector2Int>();
    private readonly List<Vector2Int> targets = new List<Vector2Int>();
    private readonly HashSet<Vector2Int> targetLookup = new HashSet<Vector2Int>();
    private System.Random random;
    private Vector2Int playerPosition;
    private bool hasDesignBlueprint;
    private LevelGenerationTemplates.StructureTemplate currentStructureTemplate =
        LevelGenerationTemplates.GetStructureTemplate(DefaultArchetype);
    private string currentArchetype = DefaultArchetype;
    private string currentTargetLayout = DefaultTargetLayout;
    private string currentObstacleStyle = DefaultObstacleStyle;
    private string currentWaterStyle = DefaultWaterStyle;
    private string currentDesignNote = "";
    private string currentCorridorPlacement = "none";
    private int currentCorridorWidth;
    private string currentCorridorOrientation = "any";
    private string currentCorridorRole = "visual_only";
    private string currentCorridorPriority = "preferred";
    private string candidateCorridorOrientation = "any";
    private readonly HashSet<Vector2Int> candidateCorridorOpeningCells = new HashSet<Vector2Int>();
    public CorridorValidationResult LastCorridorValidation { get; private set; }
    private bool activeLLMQualityGate;
    private bool activeRelaxedBlueprint;
    private bool preserveZeroWaterConstraint;
    private bool preserveZeroInternalWallConstraint;
    private GenerationRuleSnapshot algorithmRuleBaseline;

    private sealed class GenerationRuleSnapshot
    {
        public int minSolutionSteps;
        public int maxSolutionSteps;
        public int minPushes;
        public int maxPushes;
        public int minWaterAreas;
        public int maxWaterAreas;
        public int minWallObstacleBlocks;
        public int maxWallObstacleBlocks;
        public int minReversePulls;
        public int maxReversePulls;
        public int maxGenerateAttempts;
        public int maxReverseStepAttempts;
        public int maxSearchStates;
    }

    public static void BeginAlgorithmTemplateRound()
    {
        algorithmTemplateRoundCounter++;
        algorithmTemplateCursor = 0;
        algorithmTemplateOrderNeedsShuffle = true;
    }

    private void Awake()
    {
        ResolveReferences();
        CaptureAlgorithmRuleBaseline();

        if (generateOnAwake)
        {
            Generate();
        }
    }

    [ContextMenu("Generate And Reload")]
    public void GenerateAndReload()
    {
        ResolveReferences();

        if (levelLoader != null)
        {
            levelLoader.levelGenerator = this;

            if (levelData != null)
            {
                levelLoader.levelData = levelData;
            }

            levelLoader.GenerateAndReload();
            return;
        }

        Generate();
    }

    public int GetLLMPlanRetryCount()
    {
        ResolveReferences();
        return rules != null ? Mathf.Max(1, rules.llmMaxPlanRetries) : 2;
    }

    public void SetLLMQualityGateRequired(bool required)
    {
        activeLLMQualityGate = required;
    }

    public void BeginLLMPlanAttempts()
    {
        ResolveReferences();
        CaptureAlgorithmRuleBaseline();
        RestoreAlgorithmRuleBaseline();
        preserveZeroWaterConstraint = false;
        preserveZeroInternalWallConstraint = false;
        ClearDesignPlan();
    }

    public void ResetAfterLLMPlanAttempts()
    {
        RestoreAlgorithmRuleBaseline();
        preserveZeroWaterConstraint = false;
        preserveZeroInternalWallConstraint = false;
        ClearDesignPlan();
    }

    public void ClearDesignPlan(bool keepLLMQualityGate = false)
    {
        hasDesignBlueprint = false;
        activeLLMQualityGate = keepLLMQualityGate;
        activeRelaxedBlueprint = false;
        currentArchetype = DefaultArchetype;
        currentTargetLayout = DefaultTargetLayout;
        currentObstacleStyle = DefaultObstacleStyle;
        currentWaterStyle = DefaultWaterStyle;
        currentDesignNote = "";
        currentCorridorPlacement = "none";
        currentCorridorWidth = 0;
        currentCorridorOrientation = "any";
        currentCorridorRole = "visual_only";
        currentCorridorPriority = "preferred";
        candidateCorridorOpeningCells.Clear();
        LastCorridorValidation = null;
        currentStructureTemplate = LevelGenerationTemplates.GetStructureTemplate(DefaultArchetype);
    }

    public bool Generate()
    {
        ResolveReferences();

        if (!CanGenerate())
        {
            return false;
        }

        random = rules.useFixedSeed
            ? new System.Random(rules.seed)
            : new System.Random(GetRuntimeSeed());

        if (activeLLMQualityGate && hasDesignBlueprint)
        {
            currentStructureTemplate = LevelGenerationTemplates.GetStructureTemplate(currentArchetype, random);
        }
        else if (!activeLLMQualityGate)
        {
            ApplyAlgorithmDesignBlueprint();
        }

        if (TryGenerateWithCurrentRules("strict", rules.maxGenerateAttempts, true))
        {
            return true;
        }

        if ((hasDesignBlueprint || activeLLMQualityGate) && TryGenerateRelaxedBlueprint())
        {
            return true;
        }

        Debug.LogWarning("LevelGenerator: Failed to generate a solvable level.");
        return false;
    }

    public IEnumerator GenerateSavedPlanRoutine(
        float budgetSeconds,
        Action<bool, string> onComplete)
    {
        ResolveReferences();

        if (!CanGenerate() || !hasDesignBlueprint || !activeLLMQualityGate)
        {
            onComplete?.Invoke(false, "generation_failed");
            yield break;
        }

        float deadline = Time.realtimeSinceStartup + Mathf.Max(1f, budgetSeconds);
        random = rules.useFixedSeed
            ? new System.Random(unchecked(rules.seed + GetRuntimeSeed()))
            : new System.Random(GetRuntimeSeed());

        const float FrameBudgetSeconds = 0.008f;
        float frameSliceStartedAt = Time.realtimeSinceStartup;
        for (int attempt = 0; attempt < rules.maxGenerateAttempts; attempt++)
        {
            if (Time.realtimeSinceStartup >= deadline)
            {
                onComplete?.Invoke(false, "generation_timed_out");
                yield break;
            }

            bool generated;
            try
            {
                generated = TryGenerateWithCurrentRules(
                    "saved-blueprint-strict",
                    1,
                    false
                );
            }
            catch (Exception exception)
            {
                Debug.LogException(exception);
                onComplete?.Invoke(false, "generation_failed");
                yield break;
            }

            if (Time.realtimeSinceStartup >= deadline)
            {
                onComplete?.Invoke(false, "generation_timed_out");
                yield break;
            }

            if (generated)
            {
                onComplete?.Invoke(true, "");
                yield break;
            }

            if (Time.realtimeSinceStartup - frameSliceStartedAt >= FrameBudgetSeconds)
            {
                yield return null;
                frameSliceStartedAt = Time.realtimeSinceStartup;
            }
        }

        int originalMinSolutionSteps = rules.minSolutionSteps;
        int originalMaxSolutionSteps = rules.maxSolutionSteps;
        int originalMinPushes = rules.minPushes;
        int originalMaxPushes = rules.maxPushes;
        int originalMinWaterAreas = rules.minWaterAreas;
        int originalMaxWaterAreas = rules.maxWaterAreas;
        int originalMinWallObstacleBlocks = rules.minWallObstacleBlocks;
        int originalMaxWallObstacleBlocks = rules.maxWallObstacleBlocks;
        int originalMinReversePulls = rules.minReversePulls;
        int originalMaxReversePulls = rules.maxReversePulls;
        bool originalHasDesignBlueprint = hasDesignBlueprint;
        bool originalActiveLLMQualityGate = activeLLMQualityGate;
        bool originalActiveRelaxedBlueprint = activeRelaxedBlueprint;
        bool relaxedGenerated = false;
        string failureCode = "generation_failed";

        activeRelaxedBlueprint = true;
        rules.minSolutionSteps = Mathf.Max(12, originalMinSolutionSteps - 10);
        rules.maxSolutionSteps = Mathf.Max(rules.minSolutionSteps, originalMaxSolutionSteps + 20);
        rules.minPushes = Mathf.Max(4, originalMinPushes - 6);
        rules.maxPushes = Mathf.Max(rules.minPushes, originalMaxPushes + 10);
        rules.minReversePulls = Mathf.Max(8, originalMinReversePulls - 10);
        rules.maxReversePulls = Mathf.Max(rules.minReversePulls, originalMaxReversePulls);

        frameSliceStartedAt = Time.realtimeSinceStartup;
        for (int attempt = 0; attempt < rules.maxGenerateAttempts; attempt++)
        {
            if (Time.realtimeSinceStartup >= deadline)
            {
                failureCode = "generation_timed_out";
                break;
            }

            try
            {
                relaxedGenerated = TryGenerateWithCurrentRules(
                    "saved-blueprint-relaxed",
                    1,
                    false
                );
            }
            catch (Exception exception)
            {
                Debug.LogException(exception);
                failureCode = "generation_failed";
                break;
            }

            if (Time.realtimeSinceStartup >= deadline)
            {
                relaxedGenerated = false;
                failureCode = "generation_timed_out";
                break;
            }

            if (relaxedGenerated)
            {
                break;
            }

            if (Time.realtimeSinceStartup - frameSliceStartedAt >= FrameBudgetSeconds)
            {
                yield return null;
                frameSliceStartedAt = Time.realtimeSinceStartup;
            }
        }

        RestoreGenerationRules(
            originalMinSolutionSteps,
            originalMaxSolutionSteps,
            originalMinPushes,
            originalMaxPushes,
            originalMinWaterAreas,
            originalMaxWaterAreas,
            originalMinWallObstacleBlocks,
            originalMaxWallObstacleBlocks,
            originalMinReversePulls,
            originalMaxReversePulls,
            originalHasDesignBlueprint,
            originalActiveLLMQualityGate
        );
        activeRelaxedBlueprint = originalActiveRelaxedBlueprint;
        onComplete?.Invoke(relaxedGenerated, relaxedGenerated ? "" : failureCode);
    }

    public bool GenerateAlgorithmFallbackAfterLLM()
    {
        ResolveReferences();
        CaptureAlgorithmRuleBaseline();
        RestoreAlgorithmRuleBaseline();
        ClearDesignPlan();

        if (!CanGenerate())
        {
            return false;
        }

        int originalMinSolutionSteps = rules.minSolutionSteps;
        int originalMaxSolutionSteps = rules.maxSolutionSteps;
        int originalMinPushes = rules.minPushes;
        int originalMaxPushes = rules.maxPushes;
        int originalMinWaterAreas = rules.minWaterAreas;
        int originalMaxWaterAreas = rules.maxWaterAreas;
        int originalMinWallObstacleBlocks = rules.minWallObstacleBlocks;
        int originalMaxWallObstacleBlocks = rules.maxWallObstacleBlocks;
        int originalMinReversePulls = rules.minReversePulls;
        int originalMaxReversePulls = rules.maxReversePulls;

        rules.minSolutionSteps = 10;
        rules.maxSolutionSteps = Mathf.Max(45, originalMaxSolutionSteps + 20);
        rules.minPushes = 4;
        rules.maxPushes = Mathf.Max(30, originalMaxPushes + 12);
        rules.minWaterAreas = preserveZeroWaterConstraint ? 0 : 1;
        rules.maxWaterAreas = preserveZeroWaterConstraint
            ? 0
            : Mathf.Max(1, originalMaxWaterAreas);
        rules.minWallObstacleBlocks = preserveZeroInternalWallConstraint ? 0 : 1;
        rules.maxWallObstacleBlocks = preserveZeroInternalWallConstraint
            ? 0
            : Mathf.Max(1, originalMaxWallObstacleBlocks);
        rules.minReversePulls = Mathf.Max(4, originalMinReversePulls - 12);
        rules.maxReversePulls = Mathf.Max(rules.minReversePulls, originalMaxReversePulls + 12);

        random = rules.useFixedSeed
            ? new System.Random(rules.seed)
            : new System.Random(GetRuntimeSeed());
        ApplyAlgorithmDesignBlueprint();

        Debug.LogWarning(
            "LevelGenerator: LLM plans were valid, but templates could not realize them."
            + " Running one cleared algorithm fallback:"
            + " solutionSteps=" + rules.minSolutionSteps + "-" + rules.maxSolutionSteps
            + ", pushes=" + rules.minPushes + "-" + rules.maxPushes
            + ", waterAreas=" + rules.minWaterAreas + "-" + rules.maxWaterAreas
            + ", wallObstacleBlocks=" + rules.minWallObstacleBlocks + "-" + rules.maxWallObstacleBlocks
            + ", reversePulls=" + rules.minReversePulls + "-" + rules.maxReversePulls
        );

        bool generated = TryGenerateWithCurrentRules(
            "llm-success-algorithm-fallback",
            rules.maxGenerateAttempts,
            true
        );

        RestoreGenerationRules(
            originalMinSolutionSteps,
            originalMaxSolutionSteps,
            originalMinPushes,
            originalMaxPushes,
            originalMinWaterAreas,
            originalMaxWaterAreas,
            originalMinWallObstacleBlocks,
            originalMaxWallObstacleBlocks,
            originalMinReversePulls,
            originalMaxReversePulls,
            hasDesignBlueprint,
            false
        );

        return generated;
    }

    private bool TryGenerateRelaxedBlueprint()
    {
        int originalMinSolutionSteps = rules.minSolutionSteps;
        int originalMaxSolutionSteps = rules.maxSolutionSteps;
        int originalMinPushes = rules.minPushes;
        int originalMaxPushes = rules.maxPushes;
        int originalMinWaterAreas = rules.minWaterAreas;
        int originalMaxWaterAreas = rules.maxWaterAreas;
        int originalMinWallObstacleBlocks = rules.minWallObstacleBlocks;
        int originalMaxWallObstacleBlocks = rules.maxWallObstacleBlocks;
        int originalMinReversePulls = rules.minReversePulls;
        int originalMaxReversePulls = rules.maxReversePulls;
        bool originalHasDesignBlueprint = hasDesignBlueprint;
        bool originalActiveLLMQualityGate = activeLLMQualityGate;
        bool originalActiveRelaxedBlueprint = activeRelaxedBlueprint;

        activeRelaxedBlueprint = true;
        rules.minSolutionSteps = Mathf.Max(12, originalMinSolutionSteps - 10);
        rules.maxSolutionSteps = Mathf.Max(rules.minSolutionSteps, originalMaxSolutionSteps + 20);
        rules.minPushes = Mathf.Max(4, originalMinPushes - 6);
        rules.maxPushes = Mathf.Max(rules.minPushes, originalMaxPushes + 10);
        rules.minReversePulls = Mathf.Max(8, originalMinReversePulls - 10);
        rules.maxReversePulls = Mathf.Max(rules.minReversePulls, originalMaxReversePulls);

        if (logGenerationResult)
        {
            Debug.LogWarning(
                "LevelGenerator: Strict LLM blueprint failed. Retrying relaxed blueprint:"
                + " solutionSteps=" + rules.minSolutionSteps + "-" + rules.maxSolutionSteps
                + ", pushes=" + rules.minPushes + "-" + rules.maxPushes
                + ", reversePulls=" + rules.minReversePulls + "-" + rules.maxReversePulls
                + ", qualityScore>=" + rules.llmRelaxedMinimumQualityScore
                + ", structureSimilarity<" + rules.llmRelaxedStructureSimilarityThreshold
            );
        }

        if (TryGenerateWithCurrentRules("relaxed-blueprint", rules.maxGenerateAttempts, true))
        {
            RestoreGenerationRules(
                originalMinSolutionSteps,
                originalMaxSolutionSteps,
                originalMinPushes,
                originalMaxPushes,
                originalMinWaterAreas,
                originalMaxWaterAreas,
                originalMinWallObstacleBlocks,
                originalMaxWallObstacleBlocks,
                originalMinReversePulls,
                originalMaxReversePulls,
                originalHasDesignBlueprint,
                originalActiveLLMQualityGate
            );
            activeRelaxedBlueprint = originalActiveRelaxedBlueprint;
            return true;
        }

        RestoreGenerationRules(
            originalMinSolutionSteps,
            originalMaxSolutionSteps,
            originalMinPushes,
            originalMaxPushes,
            originalMinWaterAreas,
            originalMaxWaterAreas,
            originalMinWallObstacleBlocks,
            originalMaxWallObstacleBlocks,
            originalMinReversePulls,
            originalMaxReversePulls,
            originalHasDesignBlueprint,
            originalActiveLLMQualityGate
        );
        activeRelaxedBlueprint = originalActiveRelaxedBlueprint;

        return false;
    }

    private void CaptureAlgorithmRuleBaseline()
    {
        if (algorithmRuleBaseline != null || rules == null)
        {
            return;
        }

        algorithmRuleBaseline = new GenerationRuleSnapshot
        {
            minSolutionSteps = rules.minSolutionSteps,
            maxSolutionSteps = rules.maxSolutionSteps,
            minPushes = rules.minPushes,
            maxPushes = rules.maxPushes,
            minWaterAreas = rules.minWaterAreas,
            maxWaterAreas = rules.maxWaterAreas,
            minWallObstacleBlocks = rules.minWallObstacleBlocks,
            maxWallObstacleBlocks = rules.maxWallObstacleBlocks,
            minReversePulls = rules.minReversePulls,
            maxReversePulls = rules.maxReversePulls,
            maxGenerateAttempts = rules.maxGenerateAttempts,
            maxReverseStepAttempts = rules.maxReverseStepAttempts,
            maxSearchStates = levelSolver != null ? levelSolver.maxSearchStates : 0
        };
    }

    private void RestoreAlgorithmRuleBaseline()
    {
        if (algorithmRuleBaseline == null || rules == null)
        {
            return;
        }

        rules.minSolutionSteps = algorithmRuleBaseline.minSolutionSteps;
        rules.maxSolutionSteps = algorithmRuleBaseline.maxSolutionSteps;
        rules.minPushes = algorithmRuleBaseline.minPushes;
        rules.maxPushes = algorithmRuleBaseline.maxPushes;
        rules.minWaterAreas = algorithmRuleBaseline.minWaterAreas;
        rules.maxWaterAreas = algorithmRuleBaseline.maxWaterAreas;
        rules.minWallObstacleBlocks = algorithmRuleBaseline.minWallObstacleBlocks;
        rules.maxWallObstacleBlocks = algorithmRuleBaseline.maxWallObstacleBlocks;
        rules.minReversePulls = algorithmRuleBaseline.minReversePulls;
        rules.maxReversePulls = algorithmRuleBaseline.maxReversePulls;
        rules.maxGenerateAttempts = algorithmRuleBaseline.maxGenerateAttempts;
        rules.maxReverseStepAttempts = algorithmRuleBaseline.maxReverseStepAttempts;

        if (levelSolver != null && algorithmRuleBaseline.maxSearchStates > 0)
        {
            levelSolver.maxSearchStates = algorithmRuleBaseline.maxSearchStates;
        }
    }

    private struct ReversePullAction
    {
        public readonly int boxIndex;
        public readonly Vector2Int direction;

        public ReversePullAction(int boxIndex, Vector2Int direction)
        {
            this.boxIndex = boxIndex;
            this.direction = direction;
        }
    }

    private void RestoreGenerationRules(
        int minSolutionSteps,
        int maxSolutionSteps,
        int minPushes,
        int maxPushes,
        int minWaterAreas,
        int maxWaterAreas,
        int minWallObstacleBlocks,
        int maxWallObstacleBlocks,
        int minReversePulls,
        int maxReversePulls,
        bool restoreDesignBlueprint,
        bool restoreActiveLLMQualityGate)
    {
        rules.minSolutionSteps = minSolutionSteps;
        rules.maxSolutionSteps = maxSolutionSteps;
        rules.minPushes = minPushes;
        rules.maxPushes = maxPushes;
        rules.minWaterAreas = minWaterAreas;
        rules.maxWaterAreas = maxWaterAreas;
        rules.minWallObstacleBlocks = minWallObstacleBlocks;
        rules.maxWallObstacleBlocks = maxWallObstacleBlocks;
        rules.minReversePulls = minReversePulls;
        rules.maxReversePulls = maxReversePulls;
        hasDesignBlueprint = restoreDesignBlueprint;
        activeLLMQualityGate = restoreActiveLLMQualityGate;
    }

    private void ResolveReferences()
    {
        if (rules == null)
        {
            rules = FindObjectOfType<LevelGenerationRules>();
        }

        if (levelLoader == null)
        {
            levelLoader = FindObjectOfType<LevelLoader>();
        }

        if (levelData == null && levelLoader != null)
        {
            levelData = levelLoader.levelData;
        }

        if (levelData == null)
        {
            levelData = FindObjectOfType<LevelData>();
        }

        if (levelSolver == null)
        {
            levelSolver = FindObjectOfType<LevelSolver>();
        }

        if (levelSolver == null)
        {
            levelSolver = gameObject.AddComponent<LevelSolver>();
            levelSolver.parseOnStart = false;
            levelSolver.solveOnStart = false;
        }
    }

    private bool CanGenerate()
    {
        if (rules == null)
        {
            Debug.LogWarning("LevelGenerator: LevelGenerationRules is missing.");
            return false;
        }

        if (levelData == null)
        {
            Debug.LogWarning("LevelGenerator: LevelData is missing.");
            return false;
        }

        if (!rules.IsValid())
        {
            Debug.LogWarning("LevelGenerator: LevelGenerationRules contains invalid values.");
            return false;
        }

        int playableCells = (rules.width - 2) * (rules.height - 2);
        int minimumRequiredCells = rules.boxCount * 2 + 1;

        if (playableCells < minimumRequiredCells)
        {
            Debug.LogWarning("LevelGenerator: Map is too small for the requested box count.");
            return false;
        }

        return true;
    }
}
