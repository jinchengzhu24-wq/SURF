using System.Collections.Generic;
using UnityEngine;

[DefaultExecutionOrder(-100)]
public class LevelGenerator : MonoBehaviour
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
                + ", targetDistance=" + targetDistance
                + ", structureSimilarity=" + structureSimilarity;
        }
    }

    private sealed class GeneratedCandidate
    {
        public readonly string[] rows;
        public readonly int attempt;
        public readonly LevelQualityReport quality;

        public GeneratedCandidate(string[] rows, int attempt, LevelQualityReport quality)
        {
            this.rows = rows;
            this.attempt = attempt;
            this.quality = quality;
        }
    }

    private static readonly Queue<string> recentFullLevelSignatures = new Queue<string>();
    private static readonly HashSet<string> recentFullLevelLookup = new HashSet<string>();
    private static readonly Queue<string> recentStructureSignatures = new Queue<string>();
    private static readonly HashSet<string> recentStructureLookup = new HashSet<string>();
    private static int runtimeSeedCounter;

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
    private bool activeLLMQualityGate;

    private void Awake()
    {
        if (generateOnAwake)
        {
            Generate();
        }
    }

    [ContextMenu("Generate And Reload")]
    public void GenerateAndReload()
    {
        if (!Generate())
        {
            return;
        }

        if (levelLoader != null)
        {
            levelLoader.levelData = levelData;
            levelLoader.LoadLevel();
        }
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

    public void ClearDesignPlan(bool keepLLMQualityGate = false)
    {
        hasDesignBlueprint = false;
        activeLLMQualityGate = keepLLMQualityGate;
        currentArchetype = DefaultArchetype;
        currentTargetLayout = DefaultTargetLayout;
        currentObstacleStyle = DefaultObstacleStyle;
        currentWaterStyle = DefaultWaterStyle;
        currentDesignNote = "";
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

        rules.minSolutionSteps = Mathf.Max(12, originalMinSolutionSteps - 8);
        rules.maxSolutionSteps = Mathf.Max(rules.minSolutionSteps, originalMaxSolutionSteps + 12);
        rules.minPushes = Mathf.Max(6, originalMinPushes - 4);
        rules.maxPushes = Mathf.Max(rules.minPushes, originalMaxPushes + 8);
        rules.minReversePulls = Mathf.Max(6, originalMinReversePulls - 8);
        rules.maxReversePulls = Mathf.Max(rules.minReversePulls, originalMaxReversePulls + 8);

        if (logGenerationResult)
        {
            Debug.LogWarning(
                "LevelGenerator: Strict LLM blueprint failed. Retrying relaxed blueprint:"
                + " solutionSteps=" + rules.minSolutionSteps + "-" + rules.maxSolutionSteps
                + ", pushes=" + rules.minPushes + "-" + rules.maxPushes
                + ", reversePulls=" + rules.minReversePulls + "-" + rules.maxReversePulls
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
            return true;
        }

        hasDesignBlueprint = false;
        rules.minSolutionSteps = 10;
        rules.maxSolutionSteps = Mathf.Max(45, originalMaxSolutionSteps + 20);
        rules.minPushes = 4;
        rules.maxPushes = Mathf.Max(30, originalMaxPushes + 12);
        rules.minWaterAreas = activeLLMQualityGate ? 1 : 0;
        rules.maxWaterAreas = Mathf.Max(1, originalMaxWaterAreas);
        rules.minWallObstacleBlocks = activeLLMQualityGate ? 1 : 0;
        rules.maxWallObstacleBlocks = Mathf.Max(1, originalMaxWallObstacleBlocks);
        rules.minReversePulls = Mathf.Max(4, originalMinReversePulls - 12);
        rules.maxReversePulls = Mathf.Max(rules.minReversePulls, originalMaxReversePulls + 12);

        if (logGenerationResult)
        {
            Debug.LogWarning(
                "LevelGenerator: Relaxed blueprint failed. Retrying algorithm fallback:"
                + " solutionSteps=" + rules.minSolutionSteps + "-" + rules.maxSolutionSteps
                + ", pushes=" + rules.minPushes + "-" + rules.maxPushes
                + ", waterAreas=" + rules.minWaterAreas + "-" + rules.maxWaterAreas
                + ", wallObstacleBlocks=" + rules.minWallObstacleBlocks + "-" + rules.maxWallObstacleBlocks
                + ", reversePulls=" + rules.minReversePulls + "-" + rules.maxReversePulls
            );
        }

        bool generatedFallback = TryGenerateWithCurrentRules("algorithm-fallback", rules.maxGenerateAttempts, true);
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

        return generatedFallback;
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

    private bool TryGenerateWithCurrentRules(string mode, int maxAttempts, bool logRejections)
    {
        float modeStartedAt = Time.realtimeSinceStartup;
        int rejectedBySolve = 0;
        int rejectedByDifficulty = 0;
        int rejectedByParse = 0;
        int rejectedByRepeat = 0;
        int rejectedByQuality = 0;
        int rejectedByObstacleInfluence = 0;
        int scoredCandidates = 0;
        int selectableCandidates = 0;
        int qualifiedCandidates = 0;
        int candidatesSinceFirstQualified = 0;
        int[] candidateFailures = new int[(int)CandidateFailureReason.BuildRows + 1];
        int candidateSampleTarget = GetCandidateSampleTarget();
        GeneratedCandidate bestCandidate = null;
        GeneratedCandidate bestQualifiedCandidate = null;

        for (int attempt = 1; attempt <= maxAttempts; attempt++)
        {
            if (!TryCreateCandidate(out string[] rows, out int reversePulls, out CandidateFailureReason failureReason))
            {
                candidateFailures[(int)failureReason]++;
                continue;
            }

            if (IsRecentlyGeneratedLayout(rows, RequiresLLMQualityGate()))
            {
                rejectedByRepeat++;
                continue;
            }

            levelData.rows = rows;
            levelSolver.levelData = levelData;

            if (!levelSolver.ParseLevel())
            {
                rejectedByParse++;
                continue;
            }

            if (!levelSolver.CanSolve(out int searchedStates, out int solutionSteps, out int pushCount))
            {
                rejectedBySolve++;
                continue;
            }

            if (solutionSteps < GetEffectiveMinSolutionSteps()
                || solutionSteps > rules.maxSolutionSteps
                || pushCount < GetEffectiveMinPushes()
                || pushCount > rules.maxPushes)
            {
                rejectedByDifficulty++;
                continue;
            }

            LevelQualityReport quality = BuildQualityReport(
                rows,
                reversePulls,
                searchedStates,
                solutionSteps,
                pushCount
            );
            scoredCandidates++;

            if (!MeetsRequiredObstacleInfluence(quality))
            {
                rejectedByObstacleInfluence++;
                rejectedByQuality++;
                continue;
            }

            GeneratedCandidate candidate = new GeneratedCandidate(CloneRows(rows), attempt, quality);
            selectableCandidates++;

            if (bestCandidate == null || IsBetterCandidate(candidate, bestCandidate))
            {
                bestCandidate = candidate;
            }

            if (MeetsRequiredQuality(quality))
            {
                qualifiedCandidates++;

                if (bestQualifiedCandidate == null || IsBetterCandidate(candidate, bestQualifiedCandidate))
                {
                    bestQualifiedCandidate = candidate;
                }
            }
            else
            {
                rejectedByQuality++;
            }

            if (RequiresLLMQualityGate() && bestQualifiedCandidate != null)
            {
                candidatesSinceFirstQualified++;
            }

            if (ShouldStopCollectingCandidates(
                    selectableCandidates,
                    qualifiedCandidates,
                    candidatesSinceFirstQualified,
                    candidateSampleTarget))
            {
                break;
            }
        }

        GeneratedCandidate selectedCandidate = bestQualifiedCandidate;

        if (selectedCandidate == null && !RequiresLLMQualityGate())
        {
            selectedCandidate = bestCandidate;
        }

        if (selectedCandidate != null)
        {
            AcceptCandidate(
                selectedCandidate,
                mode,
                scoredCandidates,
                selectableCandidates,
                qualifiedCandidates,
                GetElapsedMilliseconds(modeStartedAt)
            );
            return true;
        }

        if (logRejections && logGenerationResult)
        {
            Debug.LogWarning(
                "LevelGenerator generation mode failed:"
                + " mode=" + mode
                + ", candidateFailures=" + FormatCandidateFailures(candidateFailures)
                + ", rejectedByParse=" + rejectedByParse
                + ", rejectedBySolve=" + rejectedBySolve
                + ", rejectedByDifficulty=" + rejectedByDifficulty
                + ", rejectedByRepeat=" + rejectedByRepeat
                + ", rejectedByQuality=" + rejectedByQuality
                + ", rejectedByObstacleInfluence=" + rejectedByObstacleInfluence
                + ", scoredCandidates=" + scoredCandidates
                + ", selectableCandidates=" + selectableCandidates
                + ", qualifiedCandidates=" + qualifiedCandidates
                + ", elapsedMs=" + GetElapsedMilliseconds(modeStartedAt)
            );
        }

        return false;
    }

    private int GetCandidateSampleTarget()
    {
        return RequiresLLMQualityGate()
            ? Mathf.Max(1, rules.llmCandidateSampleCount)
            : Mathf.Max(1, rules.algorithmCandidateSampleCount);
    }

    private bool ShouldStopCollectingCandidates(
        int selectableCandidates,
        int qualifiedCandidates,
        int candidatesSinceFirstQualified,
        int candidateSampleTarget)
    {
        if (RequiresLLMQualityGate())
        {
            return qualifiedCandidates > 0 && candidatesSinceFirstQualified > candidateSampleTarget;
        }

        return selectableCandidates >= candidateSampleTarget;
    }

    private bool IsBetterCandidate(GeneratedCandidate candidate, GeneratedCandidate currentBest)
    {
        if (candidate.quality.score != currentBest.quality.score)
        {
            return candidate.quality.score > currentBest.quality.score;
        }

        if (candidate.quality.solutionSteps != currentBest.quality.solutionSteps)
        {
            return candidate.quality.solutionSteps > currentBest.quality.solutionSteps;
        }

        return candidate.quality.pushes > currentBest.quality.pushes;
    }

    private bool MeetsRequiredQuality(LevelQualityReport quality)
    {
        if (RequiresLLMQualityGate())
        {
            return quality.waterTiles >= rules.llmMinimumWaterTiles
                && quality.surroundedWallCount >= rules.llmMinimumSurroundedWalls
                && quality.structureSimilarity < rules.recentStructureSimilarityThreshold
                && quality.score >= rules.llmMinimumQualityScore;
        }

        return MeetsRequiredObstacleInfluence(quality)
            && quality.score >= rules.algorithmMinimumQualityScore;
    }

    private bool MeetsRequiredObstacleInfluence(LevelQualityReport quality)
    {
        return RequiresLLMQualityGate()
            || quality.obstacleInfluence >= rules.algorithmMinimumObstacleInfluence;
    }

    private void AcceptCandidate(
        GeneratedCandidate candidate,
        string mode,
        int scoredCandidates,
        int selectableCandidates,
        int qualifiedCandidates,
        int elapsedMs)
    {
        levelData.rows = CloneRows(candidate.rows);
        levelSolver.levelData = levelData;
        lastAttempts = candidate.attempt;
        lastReversePulls = candidate.quality.reversePulls;
        lastSearchedStates = candidate.quality.searchedStates;
        lastSolutionSteps = candidate.quality.solutionSteps;
        lastPushes = candidate.quality.pushes;
        RememberGeneratedLayout(levelData.rows);

        if (levelLoader != null)
        {
            levelLoader.levelData = levelData;
        }

        if (logGenerationResult)
        {
            Debug.Log(
                "LevelGenerator generated level:"
                + " mode=" + mode
                + ", attempts=" + lastAttempts
                + ", scoredCandidates=" + scoredCandidates
                + ", selectableCandidates=" + selectableCandidates
                + ", qualifiedCandidates=" + qualifiedCandidates
                + ", elapsedMs=" + elapsedMs
                + ", " + candidate.quality.Summary()
            );
        }
    }

    private LevelQualityReport BuildQualityReport(
        string[] rows,
        int reversePulls,
        int searchedStates,
        int solutionSteps,
        int pushes)
    {
        LevelQualityReport quality = new LevelQualityReport();
        quality.solutionSteps = solutionSteps;
        quality.pushes = pushes;
        quality.reversePulls = reversePulls;
        quality.searchedStates = searchedStates;
        quality.waterTiles = CountTiles(rows, Water);
        quality.waterAreas = CountWaterAreas(rows);
        quality.surroundedWallCount = CountSurroundedWalls(rows);
        quality.obstacleInfluence = CountObstacleInfluence(rows);
        quality.targetDistance = GetTargetDistance(rows);
        quality.structureSimilarity = GetMaxRecentStructureSimilarity(rows);
        quality.score = GetQualityScore(quality);
        return quality;
    }

    private int GetQualityScore(LevelQualityReport quality)
    {
        int score = 0;
        score += quality.solutionSteps * 3;
        score += quality.pushes * 10;
        score += quality.reversePulls * 2;
        score += Mathf.Min(quality.searchedStates / 100, 40);
        score += Mathf.Min(quality.waterTiles, 12) * 3;
        score += quality.waterAreas * 20;
        score += Mathf.Min(quality.surroundedWallCount, 3) * 45;
        score += Mathf.Min(quality.obstacleInfluence, 8) * 18;
        score += quality.targetDistance * 6;
        score -= quality.structureSimilarity;
        return score;
    }

    private int GetEffectiveMinSolutionSteps()
    {
        if (RequiresLLMQualityGate())
        {
            return rules.minSolutionSteps;
        }

        return Mathf.Max(rules.minSolutionSteps, rules.algorithmPreferredMinSolutionSteps);
    }

    private int GetEffectiveMinPushes()
    {
        if (RequiresLLMQualityGate())
        {
            return rules.minPushes;
        }

        return Mathf.Max(rules.minPushes, rules.algorithmPreferredMinPushes);
    }

    private int GetEffectiveMinReversePulls()
    {
        if (RequiresLLMQualityGate())
        {
            return rules.minReversePulls;
        }

        return Mathf.Max(rules.minReversePulls, rules.algorithmPreferredMinReversePulls);
    }

    private int GetEffectiveMaxReversePulls()
    {
        return Mathf.Max(GetEffectiveMinReversePulls(), rules.maxReversePulls);
    }

    private bool RequiresLLMQualityGate()
    {
        return activeLLMQualityGate;
    }

    private int GetElapsedMilliseconds(float startedAt)
    {
        return Mathf.RoundToInt((Time.realtimeSinceStartup - startedAt) * 1000f);
    }

    private string FormatCandidateFailures(int[] candidateFailures)
    {
        List<string> parts = new List<string>();

        for (int i = 1; i < candidateFailures.Length; i++)
        {
            if (candidateFailures[i] <= 0)
            {
                continue;
            }

            parts.Add(((CandidateFailureReason)i) + "=" + candidateFailures[i]);
        }

        return parts.Count > 0 ? string.Join(", ", parts.ToArray()) : "none";
    }

    public void ApplyPlan(LevelDesignPlan plan)
    {
        if (plan == null)
        {
            return;
        }

        ResolveReferences();

        if (rules == null)
        {
            Debug.LogWarning("LevelGenerator: Cannot apply level design plan because rules are missing.");
            return;
        }

        rules.minSolutionSteps = Mathf.Max(0, plan.minSolutionSteps);
        rules.maxSolutionSteps = Mathf.Max(rules.minSolutionSteps, plan.maxSolutionSteps);
        if (plan.minPushes > 0 || plan.maxPushes > 0)
        {
            rules.minPushes = Mathf.Max(0, plan.minPushes);
            rules.maxPushes = Mathf.Max(rules.minPushes, plan.maxPushes);
        }

        rules.minWaterAreas = Mathf.Max(0, plan.minWaterAreas);
        rules.maxWaterAreas = Mathf.Max(rules.minWaterAreas, plan.maxWaterAreas);
        rules.minWallObstacleBlocks = Mathf.Max(0, plan.minWallObstacleBlocks);
        rules.maxWallObstacleBlocks = Mathf.Max(rules.minWallObstacleBlocks, plan.maxWallObstacleBlocks);

        if (plan.minReversePulls > 0 || plan.maxReversePulls > 0)
        {
            rules.minReversePulls = Mathf.Max(0, plan.minReversePulls);
            rules.maxReversePulls = Mathf.Max(rules.minReversePulls, plan.maxReversePulls);
        }

        currentArchetype = NormalizeArchetype(plan.archetype);
        currentTargetLayout = NormalizeTargetLayout(plan.targetLayout);
        currentObstacleStyle = NormalizeObstacleStyle(plan.obstacleStyle);
        currentWaterStyle = NormalizeWaterStyle(plan.waterStyle);
        currentDesignNote = string.IsNullOrEmpty(plan.designNote) ? "" : plan.designNote;
        currentStructureTemplate = LevelGenerationTemplates.GetStructureTemplate(currentArchetype);
        hasDesignBlueprint = true;
        activeLLMQualityGate = true;

        rules.maxGenerateAttempts = Mathf.Min(rules.maxGenerateAttempts, 450);
        rules.maxReverseStepAttempts = Mathf.Min(rules.maxReverseStepAttempts, 320);

        if (levelSolver != null)
        {
            levelSolver.maxSearchStates = Mathf.Min(levelSolver.maxSearchStates, 120000);
        }

        if (logGenerationResult)
        {
            Debug.Log(
                "LevelGenerator applied level design plan:"
                + " solutionSteps=" + rules.minSolutionSteps + "-" + rules.maxSolutionSteps
                + ", pushes=" + rules.minPushes + "-" + rules.maxPushes
                + ", waterAreas=" + rules.minWaterAreas + "-" + rules.maxWaterAreas
                + ", wallObstacleBlocks=" + rules.minWallObstacleBlocks + "-" + rules.maxWallObstacleBlocks
                + ", reversePulls=" + rules.minReversePulls + "-" + rules.maxReversePulls
                + ", archetype=" + currentArchetype
                + ", targetLayout=" + currentTargetLayout
                + ", obstacleStyle=" + currentObstacleStyle
                + ", waterStyle=" + currentWaterStyle
                + ", style=" + plan.style
                + ", designNote=" + currentDesignNote
            );
        }
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

    private bool TryCreateCandidate(
        out string[] rows,
        out int reversePulls,
        out CandidateFailureReason failureReason)
    {
        rows = null;
        reversePulls = 0;
        failureReason = CandidateFailureReason.None;

        if (!TryCreateBaseGrid(out char[,] grid))
        {
            failureReason = CandidateFailureReason.BaseGrid;
            return false;
        }

        bool placedTargets = false;

        if (hasDesignBlueprint)
        {
            if (!TryAddWallObstacleBlocks(grid))
            {
                failureReason = CandidateFailureReason.WallObstacles;
                return false;
            }

            if (!TryAddWaterAreas(grid))
            {
                failureReason = CandidateFailureReason.WaterAreas;
                return false;
            }
        }
        else
        {
            if (!TryAddWaterAreas(grid))
            {
                failureReason = CandidateFailureReason.WaterAreas;
                return false;
            }

            if (!TryPlaceTargets(grid))
            {
                failureReason = CandidateFailureReason.Targets;
                return false;
            }

            placedTargets = true;

            if (!TryAddWallObstacleBlocks(grid))
            {
                failureReason = CandidateFailureReason.WallObstacles;
                return false;
            }
        }

        if (!placedTargets && !TryPlaceTargets(grid))
        {
            failureReason = CandidateFailureReason.Targets;
            return false;
        }

        if (!TryPlacePlayer(grid))
        {
            failureReason = CandidateFailureReason.Player;
            return false;
        }

        int targetPulls = NextInclusive(GetEffectiveMinReversePulls(), GetEffectiveMaxReversePulls());

        if (!TryReversePulls(grid, targetPulls, out reversePulls))
        {
            failureReason = CandidateFailureReason.ReversePulls;
            return false;
        }

        if (!ValidateTileRules(grid, true))
        {
            failureReason = CandidateFailureReason.TileRules;
            return false;
        }

        if (!TryBuildRows(grid, out rows))
        {
            failureReason = CandidateFailureReason.BuildRows;
            return false;
        }

        return true;
    }

    private bool TryCreateBaseGrid(out char[,] grid)
    {
        grid = rules.enableIrregularOuterWalls
            ? CreateSupportedOuterShell()
            : CreateRectangularOuterShell();

        return HasEnoughGroundCells(grid)
            && AreGroundCellsConnected(grid)
            && ValidateWallTileRules(grid);
    }

    private char[,] CreateSupportedOuterShell()
    {
        if (rules.width != 12 || rules.height != 10)
        {
            return CreateRectangularOuterShell();
        }

        string[] template = LevelGenerationTemplates.OuterShellTemplates[GetOuterShellTemplateIndex()];
        bool mirrorHorizontally = random.Next(2) == 0;
        char[,] grid = new char[rules.width, rules.height];

        for (int y = 0; y < rules.height; y++)
        {
            string row = template[y];

            for (int x = 0; x < rules.width; x++)
            {
                int sourceX = mirrorHorizontally ? rules.width - 1 - x : x;
                grid[x, y] = row[sourceX];
            }
        }

        return grid;
    }

    private int GetOuterShellTemplateIndex()
    {
        if (currentArchetype == "goal_room")
        {
            return currentStructureTemplate.outerShellTemplateIndex;
        }

        if (currentArchetype == "bottleneck_corridor")
        {
            return currentStructureTemplate.outerShellTemplateIndex;
        }

        if (currentArchetype == "split_route")
        {
            return currentStructureTemplate.outerShellTemplateIndex;
        }

        if (hasDesignBlueprint)
        {
            return currentStructureTemplate.outerShellTemplateIndex;
        }

        return random.Next(LevelGenerationTemplates.OuterShellTemplates.Length);
    }

    private char[,] CreateRectangularOuterShell()
    {
        char[,] grid = new char[rules.width, rules.height];

        for (int y = 0; y < rules.height; y++)
        {
            for (int x = 0; x < rules.width; x++)
            {
                bool border = x == 0 || y == 0 || x == rules.width - 1 || y == rules.height - 1;
                grid[x, y] = border ? Wall : Ground;
            }
        }

        return grid;
    }

    private bool TryAddWaterAreas(char[,] grid)
    {
        int waterAreaCount = NextInclusive(rules.minWaterAreas, rules.maxWaterAreas);
        int placedWaterAreas = 0;

        for (int i = 0; i < waterAreaCount; i++)
        {
            if (TryAddWaterArea(grid))
            {
                placedWaterAreas++;
            }
        }

        return placedWaterAreas >= rules.minWaterAreas
            && AreGroundCellsConnected(grid)
            && ValidateTileRules(grid);
    }

    private bool TryAddWaterArea(char[,] grid)
    {
        for (int attempt = 0; attempt < 20; attempt++)
        {
            Vector2Int size = new Vector2Int(
                NextInclusive(rules.minWaterWidth, rules.maxWaterWidth),
                NextInclusive(rules.minWaterHeight, rules.maxWaterHeight)
            );

            if (!rules.IsValidWaterRectSize(size.x, size.y))
            {
                continue;
            }

            List<Vector2Int> origins = GetWaterOriginCandidates(grid, size);

            if (origins.Count == 0)
            {
                continue;
            }

            Shuffle(origins);
            origins.Sort((left, right) => GetWaterOriginScore(right, size).CompareTo(GetWaterOriginScore(left, size)));

            Vector2Int origin = origins[0];

            for (int y = origin.y; y < origin.y + size.y; y++)
            {
                for (int x = origin.x; x < origin.x + size.x; x++)
                {
                    grid[x, y] = Water;
                }
            }

            return true;
        }

        return false;
    }

    private List<Vector2Int> GetWaterOriginCandidates(char[,] grid, Vector2Int size)
    {
        List<Vector2Int> origins = new List<Vector2Int>();
        int minOriginY = 2;
        int maxOriginX = rules.width - size.x - 1;
        int maxOriginY = rules.height - size.y - 1;

        if (maxOriginX < 1 || maxOriginY < minOriginY)
        {
            return origins;
        }

        for (int y = minOriginY; y <= maxOriginY; y++)
        {
            for (int x = 1; x <= maxOriginX; x++)
            {
                Vector2Int origin = new Vector2Int(x, y);

                if (rules.IsValidWaterRect(origin, size) && CanPlaceWaterRect(grid, origin, size))
                {
                    origins.Add(origin);
                }
            }
        }

        return origins;
    }

    private int GetWaterOriginScore(Vector2Int origin, Vector2Int size)
    {
        Vector2Int center = new Vector2Int(origin.x + size.x / 2, origin.y + size.y / 2);
        int score;

        if (currentWaterStyle == "corner_pool")
        {
            score = 100 - GetDistanceToNearestPlayableCorner(center) * 8;
            return score + GetAnchorScore(center, currentStructureTemplate.waterAnchors, 80);
        }

        if (currentWaterStyle == "route_divider")
        {
            Vector2Int mapCenter = GetMapCenter();
            score = 100 - ManhattanDistance(center, mapCenter) * 6;
            return score + GetAnchorScore(center, currentStructureTemplate.waterAnchors, 80);
        }

        int sideDistance = Mathf.Min(center.x - 1, rules.width - 2 - center.x);
        int verticalCenterDistance = Mathf.Abs(center.y - GetMapCenter().y);
        score = 100 - sideDistance * 8 - verticalCenterDistance * 2;
        return score + GetAnchorScore(center, currentStructureTemplate.waterAnchors, 80);
    }

    private bool TryAddWallObstacleBlocks(char[,] grid)
    {
        if (!HasEnoughGroundCells(grid) || !AreGroundCellsConnected(grid) || !ValidateWallTileRules(grid))
        {
            return false;
        }

        int obstacleCount = NextInclusive(rules.minWallObstacleBlocks, rules.maxWallObstacleBlocks);
        int placedObstacles = 0;
        int maxAttempts = Mathf.Max(80, obstacleCount * 50);

        if (RequiresLLMQualityGate() && placedObstacles < obstacleCount)
        {
            if (TryAddSurroundedWallObstacleBlock(grid))
            {
                placedObstacles++;
            }
        }

        if (hasDesignBlueprint && placedObstacles < obstacleCount)
        {
            if (TryAddTemplateWallObstacleBlock(grid))
            {
                placedObstacles++;
            }
        }

        for (int attempt = 0; attempt < maxAttempts && placedObstacles < obstacleCount; attempt++)
        {
            if (TryAddWallObstacleBlock(grid))
            {
                placedObstacles++;
            }
        }

        return placedObstacles >= obstacleCount;
    }

    private bool TryAddSurroundedWallObstacleBlock(char[,] grid)
    {
        string[] shape = { "#" };
        List<Vector2Int> origins = GetWallObstacleOriginCandidates(
            grid,
            shape,
            GetWallObstacleShapeSize(shape)
        );

        Shuffle(origins);
        origins.Sort((left, right) => GetWallObstacleOriginScore(right, Vector2Int.one)
            .CompareTo(GetWallObstacleOriginScore(left, Vector2Int.one)));

        for (int i = 0; i < origins.Count; i++)
        {
            if (TryCommitWallObstacleShape(grid, origins[i], shape))
            {
                return true;
            }
        }

        return false;
    }

    private bool TryAddWallObstacleBlock(char[,] grid)
    {
        string[] shape = LevelGenerationTemplates.WallObstacleShapes[random.Next(LevelGenerationTemplates.WallObstacleShapes.Length)];
        Vector2Int size = GetWallObstacleShapeSize(shape);
        List<Vector2Int> origins = GetWallObstacleOriginCandidates(grid, shape, size);

        if (origins.Count == 0)
        {
            return false;
        }

        Shuffle(origins);
        origins.Sort((left, right) => GetWallObstacleOriginScore(right, size).CompareTo(GetWallObstacleOriginScore(left, size)));

        for (int i = 0; i < origins.Count; i++)
        {
            Vector2Int origin = origins[i];
            SetWallObstacleShape(grid, origin, shape, Wall);

            if (HasEnoughGroundCells(grid)
                && AreGroundCellsConnected(grid)
                && ValidateWallTileRules(grid))
            {
                return true;
            }

            SetWallObstacleShape(grid, origin, shape, Ground);
        }

        return false;
    }

    private bool TryAddTemplateWallObstacleBlock(char[,] grid)
    {
        List<int> shapeIndices = new List<int>(currentStructureTemplate.wallShapeIndices);
        Shuffle(shapeIndices);

        for (int shapeIndex = 0; shapeIndex < shapeIndices.Count; shapeIndex++)
        {
            string[] shape = GetWallObstacleShape(shapeIndices[shapeIndex]);
            Vector2Int size = GetWallObstacleShapeSize(shape);
            List<Vector2Int> origins = GetTemplateWallObstacleOrigins(size);

            Shuffle(origins);
            origins.Sort((left, right) => GetWallObstacleOriginScore(right, size).CompareTo(GetWallObstacleOriginScore(left, size)));

            for (int i = 0; i < origins.Count; i++)
            {
                if (TryCommitWallObstacleShape(grid, origins[i], shape))
                {
                    return true;
                }
            }
        }

        return false;
    }

    private string[] GetWallObstacleShape(int shapeIndex)
    {
        int index = Mathf.Abs(shapeIndex) % LevelGenerationTemplates.WallObstacleShapes.Length;
        return LevelGenerationTemplates.WallObstacleShapes[index];
    }

    private List<Vector2Int> GetTemplateWallObstacleOrigins(Vector2Int size)
    {
        List<Vector2Int> origins = new List<Vector2Int>();

        for (int i = 0; i < currentStructureTemplate.obstacleAnchors.Length; i++)
        {
            Vector2Int anchor = currentStructureTemplate.obstacleAnchors[i];

            for (int yOffset = -1; yOffset <= 1; yOffset++)
            {
                for (int xOffset = -1; xOffset <= 1; xOffset++)
                {
                    Vector2Int origin = new Vector2Int(
                        anchor.x - size.x / 2 + xOffset,
                        anchor.y - size.y / 2 + yOffset
                    );

                    if (!ContainsPosition(origins, origin))
                    {
                        origins.Add(origin);
                    }
                }
            }
        }

        return origins;
    }

    private List<Vector2Int> GetWallObstacleOriginCandidates(char[,] grid, string[] shape, Vector2Int size)
    {
        List<Vector2Int> origins = new List<Vector2Int>();
        int minX = 2;
        int minY = 2;
        int maxX = rules.width - size.x - 2;
        int maxY = rules.height - size.y - 2;

        if (maxX < minX || maxY < minY)
        {
            return origins;
        }

        for (int y = minY; y <= maxY; y++)
        {
            for (int x = minX; x <= maxX; x++)
            {
                Vector2Int origin = new Vector2Int(x, y);

                if (CanPlaceWallObstacleShape(grid, origin, shape))
                {
                    origins.Add(origin);
                }
            }
        }

        return origins;
    }

    private bool TryCommitWallObstacleShape(char[,] grid, Vector2Int origin, string[] shape)
    {
        if (!CanPlaceWallObstacleShape(grid, origin, shape))
        {
            return false;
        }

        SetWallObstacleShape(grid, origin, shape, Wall);

        if (HasEnoughGroundCells(grid)
            && AreGroundCellsConnected(grid)
            && ValidateWallTileRules(grid))
        {
            return true;
        }

        SetWallObstacleShape(grid, origin, shape, Ground);
        return false;
    }

    private int GetWallObstacleOriginScore(Vector2Int origin, Vector2Int size)
    {
        Vector2Int center = new Vector2Int(origin.x + size.x / 2, origin.y + size.y / 2);
        Vector2Int mapCenter = GetMapCenter();
        int templateScore = hasDesignBlueprint
            ? GetAnchorScore(center, currentStructureTemplate.obstacleAnchors, 80)
            : 0;
        int routeScore = GetWallObstacleRouteScore(center);

        if (currentObstacleStyle == "side_choke")
        {
            int sideDistance = Mathf.Min(center.x - 1, rules.width - 2 - center.x);
            int verticalCenterDistance = Mathf.Abs(center.y - mapCenter.y);
            return 120 - sideDistance * 10 - verticalCenterDistance * 3 + templateScore + routeScore;
        }

        if (currentObstacleStyle == "goal_guard")
        {
            int edgeDistance = GetDistanceToNearestPlayableEdge(center);
            int horizontalCenterDistance = Mathf.Abs(center.x - mapCenter.x);
            return 120 - edgeDistance * 8 - horizontalCenterDistance * 2 + templateScore + routeScore;
        }

        return 120 - ManhattanDistance(center, mapCenter) * 8 + templateScore + routeScore;
    }

    private int GetWallObstacleRouteScore(Vector2Int center)
    {
        if (targets.Count == 0)
        {
            return 0;
        }

        int nearestTargetDistance = GetNearestDistance(center, targets);
        int score = Mathf.Max(0, 72 - nearestTargetDistance * 16);

        if (IsNearTargetCorridor(center))
        {
            score += 45;
        }

        Vector2Int mapCenter = GetMapCenter();
        score += Mathf.Max(0, 28 - ManhattanDistance(center, mapCenter) * 4);
        return score;
    }

    private bool IsNearTargetCorridor(Vector2Int position)
    {
        if (targets.Count < 2)
        {
            return false;
        }

        for (int i = 0; i < targets.Count; i++)
        {
            for (int j = i + 1; j < targets.Count; j++)
            {
                if (IsNearAxisAlignedCorridor(position, targets[i], targets[j], 2))
                {
                    return true;
                }
            }
        }

        return false;
    }

    private bool CanPlaceWallObstacleShape(char[,] grid, Vector2Int origin, string[] shape)
    {
        Vector2Int size = GetWallObstacleShapeSize(shape);

        for (int y = origin.y - 1; y <= origin.y + size.y; y++)
        {
            for (int x = origin.x - 1; x <= origin.x + size.x; x++)
            {
                Vector2Int position = new Vector2Int(x, y);

                if (!rules.IsInsidePlayableArea(position) || grid[x, y] != Ground)
                {
                    return false;
                }
            }
        }

        for (int y = origin.y; y < origin.y + size.y; y++)
        {
            for (int x = origin.x; x < origin.x + size.x; x++)
            {
                if (GetWallObstacleShapeTile(shape, x - origin.x, y - origin.y) != Wall)
                {
                    continue;
                }

                Vector2Int position = new Vector2Int(x, y);

                if (targetLookup.Contains(position) || HasWaterBelow(grid, position))
                {
                    return false;
                }
            }
        }

        return true;
    }

    private void SetWallObstacleShape(char[,] grid, Vector2Int origin, string[] shape, char tile)
    {
        Vector2Int size = GetWallObstacleShapeSize(shape);

        for (int y = origin.y; y < origin.y + size.y; y++)
        {
            for (int x = origin.x; x < origin.x + size.x; x++)
            {
                if (GetWallObstacleShapeTile(shape, x - origin.x, y - origin.y) != Wall)
                {
                    continue;
                }

                grid[x, y] = tile;
            }
        }
    }

    private Vector2Int GetWallObstacleShapeSize(string[] shape)
    {
        int width = 0;

        for (int i = 0; i < shape.Length; i++)
        {
            width = Mathf.Max(width, shape[i].Length);
        }

        return new Vector2Int(width, shape.Length);
    }

    private char GetWallObstacleShapeTile(string[] shape, int x, int y)
    {
        if (y < 0 || y >= shape.Length || x < 0 || x >= shape[y].Length)
        {
            return Ground;
        }

        return shape[y][x];
    }

    private bool HasEnoughGroundCells(char[,] grid)
    {
        int requiredCells = rules.boxCount * 2 + 1;
        return CountGroundCells(grid) >= requiredCells;
    }

    private int CountGroundCells(char[,] grid)
    {
        int count = 0;

        for (int y = 1; y < rules.height - 1; y++)
        {
            for (int x = 1; x < rules.width - 1; x++)
            {
                if (grid[x, y] == Ground)
                {
                    count++;
                }
            }
        }

        return count;
    }

    private bool AreGroundCellsConnected(char[,] grid)
    {
        Vector2Int start = Vector2Int.zero;
        bool hasStart = false;
        int groundCellCount = 0;

        for (int y = 1; y < rules.height - 1; y++)
        {
            for (int x = 1; x < rules.width - 1; x++)
            {
                if (grid[x, y] != Ground)
                {
                    continue;
                }

                groundCellCount++;

                if (!hasStart)
                {
                    start = new Vector2Int(x, y);
                    hasStart = true;
                }
            }
        }

        if (!hasStart)
        {
            return false;
        }

        Queue<Vector2Int> open = new Queue<Vector2Int>();
        HashSet<Vector2Int> visited = new HashSet<Vector2Int>();

        open.Enqueue(start);
        visited.Add(start);

        while (open.Count > 0)
        {
            Vector2Int current = open.Dequeue();

            for (int i = 0; i < directions.Length; i++)
            {
                Vector2Int next = current + directions[i];

                if (!rules.IsInsidePlayableArea(next)
                    || visited.Contains(next)
                    || grid[next.x, next.y] != Ground)
                {
                    continue;
                }

                visited.Add(next);
                open.Enqueue(next);
            }
        }

        return visited.Count == groundCellCount;
    }

    private bool TryPlaceTargets(char[,] grid)
    {
        boxes.Clear();
        targets.Clear();
        targetLookup.Clear();

        List<Vector2Int> candidates = GetGroundCells(grid);

        if (candidates.Count < rules.boxCount)
        {
            return false;
        }

        if (hasDesignBlueprint && TryPlaceTargetsNearTemplateAnchors(candidates))
        {
            return true;
        }

        if (TryPlaceTargetsByLayout(candidates))
        {
            return true;
        }

        boxes.Clear();
        targets.Clear();
        targetLookup.Clear();
        Shuffle(candidates);

        for (int i = 0; i < rules.boxCount; i++)
        {
            Vector2Int position = candidates[i];
            targets.Add(position);
            targetLookup.Add(position);
            boxes.Add(position);
        }

        return true;
    }

    private bool TryPlaceTargetsByLayout(List<Vector2Int> candidates)
    {
        if (currentTargetLayout == "clustered")
        {
            return TryPlaceClusteredTargets(candidates, false);
        }

        if (currentTargetLayout == "edge_cluster")
        {
            return TryPlaceClusteredTargets(candidates, true);
        }

        if (currentTargetLayout == "split_pair")
        {
            return TryPlaceSplitTargets(candidates);
        }

        return false;
    }

    private bool TryPlaceTargetsNearTemplateAnchors(List<Vector2Int> candidates)
    {
        if (currentStructureTemplate.targetAnchors.Length < rules.boxCount)
        {
            return false;
        }

        List<Vector2Int> selected = new List<Vector2Int>();

        for (int anchorIndex = 0; anchorIndex < rules.boxCount; anchorIndex++)
        {
            Vector2Int anchor = currentStructureTemplate.targetAnchors[anchorIndex];
            int bestDistance = int.MaxValue;
            Vector2Int bestCandidate = Vector2Int.zero;
            bool foundCandidate = false;

            for (int i = 0; i < candidates.Count; i++)
            {
                Vector2Int candidate = candidates[i];

                if (ContainsPosition(selected, candidate))
                {
                    continue;
                }

                int distance = ManhattanDistance(candidate, anchor);

                if (distance >= bestDistance)
                {
                    continue;
                }

                bestDistance = distance;
                bestCandidate = candidate;
                foundCandidate = true;
            }

            if (!foundCandidate || bestDistance > 4)
            {
                return false;
            }

            selected.Add(bestCandidate);
        }

        return CommitTargetPositions(selected);
    }

    private bool TryPlaceClusteredTargets(List<Vector2Int> candidates, bool requireEdge)
    {
        List<Vector2Int> seeds = new List<Vector2Int>(candidates);
        Shuffle(seeds);

        for (int i = 0; i < seeds.Count; i++)
        {
            Vector2Int seed = seeds[i];

            if (requireEdge && !IsNearPlayableEdge(seed))
            {
                continue;
            }

            List<Vector2Int> cluster = new List<Vector2Int>();

            for (int j = 0; j < candidates.Count; j++)
            {
                Vector2Int candidate = candidates[j];

                if (requireEdge && !IsNearPlayableEdge(candidate))
                {
                    continue;
                }

                if (ManhattanDistance(seed, candidate) <= 2)
                {
                    cluster.Add(candidate);
                }
            }

            if (cluster.Count < rules.boxCount)
            {
                continue;
            }

            Shuffle(cluster);
            cluster.Sort((left, right) => ManhattanDistance(left, seed).CompareTo(ManhattanDistance(right, seed)));

            if (CommitTargetPositions(cluster))
            {
                return true;
            }
        }

        return false;
    }

    private bool TryPlaceSplitTargets(List<Vector2Int> candidates)
    {
        if (rules.boxCount != 2)
        {
            return false;
        }

        int bestDistance = -1;
        Vector2Int first = Vector2Int.zero;
        Vector2Int second = Vector2Int.zero;

        for (int i = 0; i < candidates.Count; i++)
        {
            for (int j = i + 1; j < candidates.Count; j++)
            {
                int distance = ManhattanDistance(candidates[i], candidates[j]);

                if (distance <= bestDistance)
                {
                    continue;
                }

                bestDistance = distance;
                first = candidates[i];
                second = candidates[j];
            }
        }

        if (bestDistance < Mathf.Max(4, rules.width / 3))
        {
            return false;
        }

        List<Vector2Int> selected = new List<Vector2Int>
        {
            first,
            second
        };

        Shuffle(selected);
        return CommitTargetPositions(selected);
    }

    private bool CommitTargetPositions(List<Vector2Int> selected)
    {
        if (selected.Count < rules.boxCount)
        {
            return false;
        }

        boxes.Clear();
        targets.Clear();
        targetLookup.Clear();

        for (int i = 0; i < rules.boxCount; i++)
        {
            Vector2Int position = selected[i];

            if (targetLookup.Contains(position))
            {
                return false;
            }

            targets.Add(position);
            targetLookup.Add(position);
            boxes.Add(position);
        }

        return true;
    }

    private bool TryPlacePlayer(char[,] grid)
    {
        List<Vector2Int> candidates = GetGroundCells(grid);
        Shuffle(candidates);
        candidates.Sort((left, right) => GetPlayerStartScore(right).CompareTo(GetPlayerStartScore(left)));

        for (int i = 0; i < candidates.Count; i++)
        {
            Vector2Int position = candidates[i];

            if (ContainsPosition(boxes, position) || targetLookup.Contains(position))
            {
                continue;
            }

            playerPosition = position;
            return true;
        }

        return false;
    }

    private int GetPlayerStartScore(Vector2Int position)
    {
        int score = 0;

        for (int i = 0; i < targets.Count; i++)
        {
            score += ManhattanDistance(position, targets[i]) * 4;
        }

        if (hasDesignBlueprint)
        {
            score += GetDistanceToNearestAnchor(position, currentStructureTemplate.targetAnchors) * 2;
        }

        return score;
    }

    private bool TryReversePulls(char[,] grid, int targetPulls, out int reversePulls)
    {
        reversePulls = 0;
        bool[] movedBoxes = new bool[boxes.Count];

        for (int attempt = 0; attempt < rules.maxReverseStepAttempts && reversePulls < targetPulls; attempt++)
        {
            int boxIndex = random.Next(boxes.Count);
            Vector2Int direction = directions[random.Next(directions.Length)];

            if (!TryReversePull(grid, boxIndex, direction))
            {
                continue;
            }

            movedBoxes[boxIndex] = true;
            reversePulls++;
        }

        if (reversePulls < targetPulls)
        {
            return false;
        }

        for (int i = 0; i < movedBoxes.Length; i++)
        {
            if (!movedBoxes[i] || targetLookup.Contains(boxes[i]))
            {
                return false;
            }
        }

        return !targetLookup.Contains(playerPosition);
    }

    private bool TryReversePull(char[,] grid, int boxIndex, Vector2Int direction)
    {
        Vector2Int boxPosition = boxes[boxIndex];
        Vector2Int nextBoxPosition = boxPosition - direction;
        Vector2Int nextPlayerPosition = boxPosition - new Vector2Int(direction.x * 2, direction.y * 2);

        if (targetLookup.Contains(nextBoxPosition) || targetLookup.Contains(nextPlayerPosition))
        {
            return false;
        }

        if (!CanPlayerOccupy(grid, nextBoxPosition) || !CanPlayerOccupy(grid, nextPlayerPosition))
        {
            return false;
        }

        if (ContainsPositionExcept(boxes, nextBoxPosition, boxIndex)
            || ContainsPositionExcept(boxes, nextPlayerPosition, boxIndex))
        {
            return false;
        }

        if (!CanReach(grid, playerPosition, nextBoxPosition))
        {
            return false;
        }

        boxes[boxIndex] = nextBoxPosition;
        playerPosition = nextPlayerPosition;
        return true;
    }

    private bool CanReach(char[,] grid, Vector2Int start, Vector2Int destination)
    {
        if (start == destination)
        {
            return true;
        }

        Queue<Vector2Int> open = new Queue<Vector2Int>();
        HashSet<Vector2Int> visited = new HashSet<Vector2Int>();

        open.Enqueue(start);
        visited.Add(start);

        while (open.Count > 0)
        {
            Vector2Int current = open.Dequeue();

            for (int i = 0; i < directions.Length; i++)
            {
                Vector2Int next = current + directions[i];

                if (visited.Contains(next) || !CanPlayerOccupy(grid, next) || ContainsPosition(boxes, next))
                {
                    continue;
                }

                if (next == destination)
                {
                    return true;
                }

                visited.Add(next);
                open.Enqueue(next);
            }
        }

        return false;
    }

    private bool TryBuildRows(char[,] grid, out string[] rows)
    {
        rows = new string[rules.height];

        if (ContainsPosition(boxes, playerPosition) || targetLookup.Contains(playerPosition))
        {
            return false;
        }

        for (int i = 0; i < boxes.Count; i++)
        {
            if (targetLookup.Contains(boxes[i]))
            {
                return false;
            }
        }

        for (int y = 0; y < rules.height; y++)
        {
            char[] row = new char[rules.width];

            for (int x = 0; x < rules.width; x++)
            {
                row[x] = grid[x, y];
            }

            rows[y] = new string(row);
        }

        for (int i = 0; i < targets.Count; i++)
        {
            SetRowTile(rows, targets[i], Target);
        }

        for (int i = 0; i < boxes.Count; i++)
        {
            SetRowTile(rows, boxes[i], Box);
        }

        SetRowTile(rows, playerPosition, Player);
        return true;
    }

    private void SetRowTile(string[] rows, Vector2Int position, char tile)
    {
        char[] row = rows[position.y].ToCharArray();
        row[position.x] = tile;
        rows[position.y] = new string(row);
    }

    private string[] CloneRows(string[] rows)
    {
        if (rows == null)
        {
            return null;
        }

        string[] clone = new string[rows.Length];

        for (int i = 0; i < rows.Length; i++)
        {
            clone[i] = rows[i] ?? "";
        }

        return clone;
    }

    private int CountTiles(string[] rows, char tile)
    {
        int count = 0;

        for (int y = 0; y < rows.Length; y++)
        {
            string row = rows[y] ?? "";

            for (int x = 0; x < row.Length; x++)
            {
                if (row[x] == tile)
                {
                    count++;
                }
            }
        }

        return count;
    }

    private int CountWaterAreas(string[] rows)
    {
        HashSet<Vector2Int> visited = new HashSet<Vector2Int>();
        int waterAreas = 0;

        for (int y = 0; y < rows.Length; y++)
        {
            string row = rows[y] ?? "";

            for (int x = 0; x < row.Length; x++)
            {
                Vector2Int position = new Vector2Int(x, y);

                if (row[x] != Water || visited.Contains(position))
                {
                    continue;
                }

                waterAreas++;
                VisitWaterArea(rows, position, visited);
            }
        }

        return waterAreas;
    }

    private void VisitWaterArea(string[] rows, Vector2Int start, HashSet<Vector2Int> visited)
    {
        Queue<Vector2Int> open = new Queue<Vector2Int>();
        open.Enqueue(start);
        visited.Add(start);

        while (open.Count > 0)
        {
            Vector2Int current = open.Dequeue();

            for (int i = 0; i < directions.Length; i++)
            {
                Vector2Int next = current + directions[i];

                if (visited.Contains(next) || GetRowsTile(rows, next) != Water)
                {
                    continue;
                }

                visited.Add(next);
                open.Enqueue(next);
            }
        }
    }

    private int CountSurroundedWalls(string[] rows)
    {
        int count = 0;

        for (int y = 0; y < rows.Length; y++)
        {
            string row = rows[y] ?? "";

            for (int x = 0; x < row.Length; x++)
            {
                if (IsSurroundedWallTileShape(rows, new Vector2Int(x, y)))
                {
                    count++;
                }
            }
        }

        return count;
    }

    private int GetTargetDistance(string[] rows)
    {
        List<Vector2Int> targetPositions = new List<Vector2Int>();

        for (int y = 0; y < rows.Length; y++)
        {
            string row = rows[y] ?? "";

            for (int x = 0; x < row.Length; x++)
            {
                if (row[x] == Target)
                {
                    targetPositions.Add(new Vector2Int(x, y));
                }
            }
        }

        int distance = 0;

        for (int i = 0; i < targetPositions.Count; i++)
        {
            for (int j = i + 1; j < targetPositions.Count; j++)
            {
                distance += ManhattanDistance(targetPositions[i], targetPositions[j]);
            }
        }

        return distance;
    }

    private int CountObstacleInfluence(string[] rows)
    {
        List<Vector2Int> targetPositions = GetRowsPositions(rows, Target);
        List<Vector2Int> boxPositions = GetRowsPositions(rows, Box);
        List<Vector2Int> playerPositions = GetRowsPositions(rows, Player);

        if (targetPositions.Count == 0 || boxPositions.Count == 0)
        {
            return 0;
        }

        int influence = 0;

        for (int y = 1; y < rows.Length - 1; y++)
        {
            string row = rows[y] ?? "";

            for (int x = 1; x < row.Length - 1; x++)
            {
                Vector2Int position = new Vector2Int(x, y);

                if (GetRowsTile(rows, position) != Wall)
                {
                    continue;
                }

                int tileInfluence = 0;
                int nearestTargetDistance = GetNearestDistance(position, targetPositions);
                int nearestBoxDistance = GetNearestDistance(position, boxPositions);

                if (nearestTargetDistance <= 2)
                {
                    tileInfluence += 2;
                }
                else if (nearestTargetDistance <= 3)
                {
                    tileInfluence++;
                }

                if (nearestBoxDistance <= 2)
                {
                    tileInfluence += 2;
                }
                else if (nearestBoxDistance <= 3)
                {
                    tileInfluence++;
                }

                if (IsNearAnyBoxTargetCorridor(position, boxPositions, targetPositions))
                {
                    tileInfluence += 2;
                }

                if (playerPositions.Count > 0 && GetNearestDistance(position, playerPositions) <= 4)
                {
                    tileInfluence++;
                }

                if (IsChokeInfluenceWall(rows, position))
                {
                    tileInfluence++;
                }

                influence += Mathf.Min(tileInfluence, 5);
            }
        }

        return influence;
    }

    private List<Vector2Int> GetRowsPositions(string[] rows, char tile)
    {
        List<Vector2Int> positions = new List<Vector2Int>();

        for (int y = 0; y < rows.Length; y++)
        {
            string row = rows[y] ?? "";

            for (int x = 0; x < row.Length; x++)
            {
                if (row[x] == tile)
                {
                    positions.Add(new Vector2Int(x, y));
                }
            }
        }

        return positions;
    }

    private bool IsNearAnyBoxTargetCorridor(
        Vector2Int position,
        List<Vector2Int> boxPositions,
        List<Vector2Int> targetPositions)
    {
        for (int boxIndex = 0; boxIndex < boxPositions.Count; boxIndex++)
        {
            for (int targetIndex = 0; targetIndex < targetPositions.Count; targetIndex++)
            {
                if (IsNearAxisAlignedCorridor(
                        position,
                        boxPositions[boxIndex],
                        targetPositions[targetIndex],
                        2))
                {
                    return true;
                }
            }
        }

        return false;
    }

    private bool IsChokeInfluenceWall(string[] rows, Vector2Int position)
    {
        bool leftOpen = IsRowsWalkableForInfluence(rows, position + new Vector2Int(-1, 0));
        bool rightOpen = IsRowsWalkableForInfluence(rows, position + new Vector2Int(1, 0));
        bool upOpen = IsRowsWalkableForInfluence(rows, position + new Vector2Int(0, -1));
        bool downOpen = IsRowsWalkableForInfluence(rows, position + new Vector2Int(0, 1));
        return (leftOpen && rightOpen && !upOpen && !downOpen)
            || (upOpen && downOpen && !leftOpen && !rightOpen);
    }

    private bool IsRowsWalkableForInfluence(string[] rows, Vector2Int position)
    {
        char tile = GetRowsTile(rows, position);
        return tile == Ground || tile == Player || tile == Box || tile == Target;
    }

    private bool IsSurroundedWallTileShape(string[] rows, Vector2Int position)
    {
        return GetRowsTile(rows, position) == Wall
            && HasRowsTilesAround(rows, position)
            && GetRowsTile(rows, position + new Vector2Int(0, 1)) != Water;
    }

    private bool HasRowsTilesAround(string[] rows, Vector2Int position)
    {
        for (int yOffset = -1; yOffset <= 1; yOffset++)
        {
            for (int xOffset = -1; xOffset <= 1; xOffset++)
            {
                if (xOffset == 0 && yOffset == 0)
                {
                    continue;
                }

                char tile = GetRowsTile(rows, position + new Vector2Int(xOffset, yOffset));

                if (tile == '\0' || tile == Empty)
                {
                    return false;
                }
            }
        }

        return true;
    }

    private char GetRowsTile(string[] rows, Vector2Int position)
    {
        if (rows == null || position.y < 0 || position.y >= rows.Length)
        {
            return '\0';
        }

        string row = rows[position.y] ?? "";

        if (position.x < 0 || position.x >= row.Length)
        {
            return '\0';
        }

        return row[position.x];
    }

    private List<Vector2Int> GetGroundCells(char[,] grid)
    {
        List<Vector2Int> cells = new List<Vector2Int>();

        for (int y = 1; y < rules.height - 1; y++)
        {
            for (int x = 1; x < rules.width - 1; x++)
            {
                if (grid[x, y] == Ground)
                {
                    cells.Add(new Vector2Int(x, y));
                }
            }
        }

        return cells;
    }

    private bool IsRectClear(char[,] grid, Vector2Int origin, Vector2Int size)
    {
        for (int y = origin.y; y < origin.y + size.y; y++)
        {
            for (int x = origin.x; x < origin.x + size.x; x++)
            {
                if (grid[x, y] != Ground)
                {
                    return false;
                }
            }
        }

        return true;
    }

    private bool CanPlaceWaterRect(char[,] grid, Vector2Int origin, Vector2Int size)
    {
        if (!IsRectClear(grid, origin, size))
        {
            return false;
        }

        for (int y = origin.y; y < origin.y + size.y; y++)
        {
            for (int x = origin.x; x < origin.x + size.x; x++)
            {
                if (HasWallAbove(grid, new Vector2Int(x, y)))
                {
                    return false;
                }
            }
        }

        return true;
    }

    private bool ValidateTileRules(char[,] grid, bool logFailure = false)
    {
        for (int y = 0; y < rules.height; y++)
        {
            for (int x = 0; x < rules.width; x++)
            {
                Vector2Int position = new Vector2Int(x, y);

                if (grid[x, y] == Water && HasWallAbove(grid, position))
                {
                    if (logFailure && logGenerationResult)
                    {
                        Debug.LogWarning("LevelGenerator: Water tile rejected below wall at " + position);
                    }

                    return false;
                }
            }
        }

        return ValidateWallTileRules(grid, logFailure);
    }

    private bool ValidateWallTileRules(char[,] grid, bool logFailure = false)
    {
        if (!ValidateNoParallelWallRows(grid, logFailure))
        {
            return false;
        }

        for (int y = 0; y < rules.height; y++)
        {
            for (int x = 0; x < rules.width; x++)
            {
                Vector2Int position = new Vector2Int(x, y);

                if (grid[x, y] == Wall && !IsSupportedWallTileShape(grid, position))
                {
                    if (logFailure && logGenerationResult)
                    {
                        Debug.LogWarning(
                            "LevelGenerator: Wall tile rule rejected wall at "
                            + position
                            + " neighbors="
                            + GetTileNeighborhoodDebug(grid, position)
                        );
                    }

                    return false;
                }
            }
        }

        return true;
    }

    private bool ValidateNoParallelWallRows(char[,] grid, bool logFailure = false)
    {
        for (int y = 0; y < rules.height - 1; y++)
        {
            int runStartX = -1;
            int runLength = 0;

            for (int x = 0; x < rules.width; x++)
            {
                bool parallelWall = grid[x, y] == Wall && grid[x, y + 1] == Wall;

                if (parallelWall)
                {
                    if (runLength == 0)
                    {
                        runStartX = x;
                    }

                    runLength++;
                    continue;
                }

                if (runLength >= 2)
                {
                    return RejectParallelWallRows(runStartX, y, runLength, logFailure);
                }

                runStartX = -1;
                runLength = 0;
            }

            if (runLength >= 2)
            {
                return RejectParallelWallRows(runStartX, y, runLength, logFailure);
            }
        }

        return true;
    }

    private bool RejectParallelWallRows(int startX, int y, int length, bool logFailure)
    {
        if (logFailure && logGenerationResult)
        {
            Debug.LogWarning(
                "LevelGenerator: Parallel wall rows rejected at "
                + new Vector2Int(startX, y)
                + " length="
                + length
            );
        }

        return false;
    }

    private string GetTileNeighborhoodDebug(char[,] grid, Vector2Int position)
    {
        return "["
            + GetDebugTile(grid, position + new Vector2Int(-1, -1))
            + GetDebugTile(grid, position + new Vector2Int(0, -1))
            + GetDebugTile(grid, position + new Vector2Int(1, -1))
            + "/"
            + GetDebugTile(grid, position + new Vector2Int(-1, 0))
            + GetDebugTile(grid, position)
            + GetDebugTile(grid, position + new Vector2Int(1, 0))
            + "/"
            + GetDebugTile(grid, position + new Vector2Int(-1, 1))
            + GetDebugTile(grid, position + new Vector2Int(0, 1))
            + GetDebugTile(grid, position + new Vector2Int(1, 1))
            + "]";
    }

    private char GetDebugTile(char[,] grid, Vector2Int position)
    {
        if (!rules.IsInsideMap(position))
        {
            return '!';
        }

        return grid[position.x, position.y];
    }

    private bool IsSupportedWallTileShape(char[,] grid, Vector2Int position)
    {
        if (IsSurroundedWallTileShape(grid, position))
        {
            return true;
        }

        bool up = IsWallAt(grid, position + new Vector2Int(0, -1));
        bool down = IsWallAt(grid, position + new Vector2Int(0, 1));
        bool left = IsWallAt(grid, position + new Vector2Int(-1, 0));
        bool right = IsWallAt(grid, position + new Vector2Int(1, 0));
        bool rightDown = IsWallAt(grid, position + new Vector2Int(1, 1));

        if (up && (left || right))
        {
            return true;
        }

        if (right && rightDown)
        {
            return true;
        }

        if (up || down)
        {
            return true;
        }

        return left && right;
    }

    private bool IsSurroundedWallTileShape(char[,] grid, Vector2Int position)
    {
        return IsWallAt(grid, position)
            && HasTilesAround(grid, position)
            && !IsWaterAt(grid, position + new Vector2Int(0, 1));
    }

    private bool HasTilesAround(char[,] grid, Vector2Int position)
    {
        for (int yOffset = -1; yOffset <= 1; yOffset++)
        {
            for (int xOffset = -1; xOffset <= 1; xOffset++)
            {
                if (xOffset == 0 && yOffset == 0)
                {
                    continue;
                }

                if (!HasTileAt(grid, position + new Vector2Int(xOffset, yOffset)))
                {
                    return false;
                }
            }
        }

        return true;
    }

    private bool HasTileAt(char[,] grid, Vector2Int position)
    {
        return rules.IsInsideMap(position) && grid[position.x, position.y] != Empty;
    }

    private bool IsWallAt(char[,] grid, Vector2Int position)
    {
        return rules.IsInsideMap(position) && grid[position.x, position.y] == Wall;
    }

    private bool IsWaterAt(char[,] grid, Vector2Int position)
    {
        return rules.IsInsideMap(position) && grid[position.x, position.y] == Water;
    }

    private bool HasWallAbove(char[,] grid, Vector2Int position)
    {
        Vector2Int above = position + new Vector2Int(0, -1);
        return rules.IsInsideMap(above) && grid[above.x, above.y] == Wall;
    }

    private bool HasWaterBelow(char[,] grid, Vector2Int position)
    {
        Vector2Int below = position + new Vector2Int(0, 1);
        return rules.IsInsideMap(below) && grid[below.x, below.y] == Water;
    }

    private bool CanPlayerOccupy(char[,] grid, Vector2Int position)
    {
        return rules.IsInsidePlayableArea(position)
            && grid[position.x, position.y] == Ground;
    }

    private bool ContainsPosition(List<Vector2Int> positions, Vector2Int position)
    {
        for (int i = 0; i < positions.Count; i++)
        {
            if (positions[i] == position)
            {
                return true;
            }
        }

        return false;
    }

    private bool ContainsPositionExcept(List<Vector2Int> positions, Vector2Int position, int ignoredIndex)
    {
        for (int i = 0; i < positions.Count; i++)
        {
            if (i != ignoredIndex && positions[i] == position)
            {
                return true;
            }
        }

        return false;
    }

    private string NormalizeArchetype(string value)
    {
        value = NormalizeBlueprintValue(value);

        if (value == "goal_room"
            || value == "bottleneck_corridor"
            || value == "split_route"
            || value == "open_workshop")
        {
            return value;
        }

        return DefaultArchetype;
    }

    private string NormalizeTargetLayout(string value)
    {
        value = NormalizeBlueprintValue(value);

        if (value == "clustered" || value == "split_pair" || value == "edge_cluster")
        {
            return value;
        }

        return DefaultTargetLayout;
    }

    private string NormalizeObstacleStyle(string value)
    {
        value = NormalizeBlueprintValue(value);

        if (value == "central_baffle" || value == "side_choke" || value == "goal_guard")
        {
            return value;
        }

        return DefaultObstacleStyle;
    }

    private string NormalizeWaterStyle(string value)
    {
        value = NormalizeBlueprintValue(value);

        if (value == "corner_pool" || value == "side_pool" || value == "route_divider")
        {
            return value;
        }

        return DefaultWaterStyle;
    }

    private string NormalizeBlueprintValue(string value)
    {
        return string.IsNullOrEmpty(value) ? "" : value.Trim().ToLowerInvariant();
    }

    private Vector2Int GetMapCenter()
    {
        return new Vector2Int(rules.width / 2, rules.height / 2);
    }

    private int ManhattanDistance(Vector2Int first, Vector2Int second)
    {
        return Mathf.Abs(first.x - second.x) + Mathf.Abs(first.y - second.y);
    }

    private int GetNearestDistance(Vector2Int position, List<Vector2Int> positions)
    {
        if (positions == null || positions.Count == 0)
        {
            return int.MaxValue;
        }

        int bestDistance = int.MaxValue;

        for (int i = 0; i < positions.Count; i++)
        {
            bestDistance = Mathf.Min(bestDistance, ManhattanDistance(position, positions[i]));
        }

        return bestDistance;
    }

    private bool IsNearAxisAlignedCorridor(
        Vector2Int position,
        Vector2Int first,
        Vector2Int second,
        int tolerance)
    {
        int minX = Mathf.Min(first.x, second.x) - tolerance;
        int maxX = Mathf.Max(first.x, second.x) + tolerance;
        int minY = Mathf.Min(first.y, second.y) - tolerance;
        int maxY = Mathf.Max(first.y, second.y) + tolerance;

        bool insideHorizontalRange = position.x >= minX
            && position.x <= maxX;
        bool insideVerticalRange = position.y >= minY
            && position.y <= maxY;
        bool nearHorizontalCorridor = insideHorizontalRange
            && (Mathf.Abs(position.y - first.y) <= tolerance
                || Mathf.Abs(position.y - second.y) <= tolerance);
        bool nearVerticalCorridor = insideVerticalRange
            && (Mathf.Abs(position.x - first.x) <= tolerance
                || Mathf.Abs(position.x - second.x) <= tolerance);

        return nearHorizontalCorridor || nearVerticalCorridor;
    }

    private bool IsNearPlayableEdge(Vector2Int position)
    {
        return GetDistanceToNearestPlayableEdge(position) <= 2;
    }

    private int GetAnchorScore(Vector2Int position, Vector2Int[] anchors, int maxScore)
    {
        if (!hasDesignBlueprint || anchors == null || anchors.Length == 0)
        {
            return 0;
        }

        return Mathf.Max(0, maxScore - GetDistanceToNearestAnchor(position, anchors) * 12);
    }

    private int GetDistanceToNearestAnchor(Vector2Int position, Vector2Int[] anchors)
    {
        if (anchors == null || anchors.Length == 0)
        {
            return 0;
        }

        int bestDistance = int.MaxValue;

        for (int i = 0; i < anchors.Length; i++)
        {
            bestDistance = Mathf.Min(bestDistance, ManhattanDistance(position, anchors[i]));
        }

        return bestDistance;
    }

    private int GetDistanceToNearestPlayableEdge(Vector2Int position)
    {
        int left = position.x - 1;
        int right = rules.width - 2 - position.x;
        int top = position.y - 1;
        int bottom = rules.height - 2 - position.y;
        return Mathf.Min(Mathf.Min(left, right), Mathf.Min(top, bottom));
    }

    private int GetDistanceToNearestPlayableCorner(Vector2Int position)
    {
        int topLeft = ManhattanDistance(position, new Vector2Int(1, 1));
        int topRight = ManhattanDistance(position, new Vector2Int(rules.width - 2, 1));
        int bottomLeft = ManhattanDistance(position, new Vector2Int(1, rules.height - 2));
        int bottomRight = ManhattanDistance(position, new Vector2Int(rules.width - 2, rules.height - 2));
        return Mathf.Min(Mathf.Min(topLeft, topRight), Mathf.Min(bottomLeft, bottomRight));
    }

    private void Shuffle(List<Vector2Int> positions)
    {
        for (int i = positions.Count - 1; i > 0; i--)
        {
            int swapIndex = random.Next(i + 1);
            Vector2Int value = positions[i];
            positions[i] = positions[swapIndex];
            positions[swapIndex] = value;
        }
    }

    private void Shuffle(List<int> values)
    {
        for (int i = values.Count - 1; i > 0; i--)
        {
            int swapIndex = random.Next(i + 1);
            int value = values[i];
            values[i] = values[swapIndex];
            values[swapIndex] = value;
        }
    }

    private int NextInclusive(int min, int max)
    {
        return random.Next(min, max + 1);
    }

    private int GetRuntimeSeed()
    {
        unchecked
        {
            runtimeSeedCounter++;
            return System.Environment.TickCount
                ^ (runtimeSeedCounter * 397)
                ^ (GetInstanceID() * 17);
        }
    }

    private bool IsRecentlyGeneratedLayout(string[] rows, bool rejectSimilar)
    {
        if (!rejectRecentlyGeneratedLayouts || recentGeneratedLayoutHistory <= 0 || rows == null)
        {
            return false;
        }

        string fullSignature = GetFullLevelSignature(rows);
        string structureSignature = GetStructureSignature(rows);
        int structureSimilarity = GetMaxRecentStructureSimilarity(structureSignature);

        bool repeated = recentFullLevelLookup.Contains(fullSignature)
            || recentStructureLookup.Contains(structureSignature)
            || (rejectSimilar && structureSimilarity >= rules.recentStructureSimilarityThreshold);

        if (repeated && logGenerationResult)
        {
            Debug.Log(
                "LevelGenerator rejected repeated layout:"
                + " recentGeneratedLayoutHistory=" + recentGeneratedLayoutHistory
                + ", structureSimilarity=" + structureSimilarity
                + ", threshold=" + rules.recentStructureSimilarityThreshold
            );
        }

        return repeated;
    }

    private void RememberGeneratedLayout(string[] rows)
    {
        if (!rejectRecentlyGeneratedLayouts || recentGeneratedLayoutHistory <= 0 || rows == null)
        {
            return;
        }

        RememberSignature(
            recentFullLevelSignatures,
            recentFullLevelLookup,
            GetFullLevelSignature(rows),
            recentGeneratedLayoutHistory
        );

        RememberSignature(
            recentStructureSignatures,
            recentStructureLookup,
            GetStructureSignature(rows),
            recentGeneratedLayoutHistory
        );
    }

    private static void RememberSignature(
        Queue<string> signatures,
        HashSet<string> lookup,
        string signature,
        int maxHistory)
    {
        if (string.IsNullOrEmpty(signature) || lookup.Contains(signature))
        {
            return;
        }

        signatures.Enqueue(signature);
        lookup.Add(signature);

        while (signatures.Count > maxHistory)
        {
            lookup.Remove(signatures.Dequeue());
        }
    }

    private string GetFullLevelSignature(string[] rows)
    {
        return string.Join("\n", rows);
    }

    private string GetStructureSignature(string[] rows)
    {
        System.Text.StringBuilder builder = new System.Text.StringBuilder();

        for (int y = 0; y < rows.Length; y++)
        {
            string row = rows[y];

            for (int x = 0; x < row.Length; x++)
            {
                char tile = row[x];
                builder.Append(tile == Player || tile == Box || tile == Target ? Ground : tile);
            }

            builder.Append('\n');
        }

        return builder.ToString();
    }

    private int GetMaxRecentStructureSimilarity(string[] rows)
    {
        return GetMaxRecentStructureSimilarity(GetStructureSignature(rows));
    }

    private int GetMaxRecentStructureSimilarity(string structureSignature)
    {
        if (string.IsNullOrEmpty(structureSignature) || recentStructureSignatures.Count == 0)
        {
            return 0;
        }

        int maxSimilarity = 0;

        foreach (string recentSignature in recentStructureSignatures)
        {
            maxSimilarity = Mathf.Max(
                maxSimilarity,
                GetStructureSimilarityPercent(structureSignature, recentSignature)
            );
        }

        return maxSimilarity;
    }

    private int GetStructureSimilarityPercent(string first, string second)
    {
        if (string.IsNullOrEmpty(first) || string.IsNullOrEmpty(second))
        {
            return 0;
        }

        int maxLength = Mathf.Max(first.Length, second.Length);

        if (maxLength == 0)
        {
            return 0;
        }

        int same = 0;
        int minLength = Mathf.Min(first.Length, second.Length);

        for (int i = 0; i < minLength; i++)
        {
            if (first[i] == second[i])
            {
                same++;
            }
        }

        return Mathf.RoundToInt((same * 100f) / maxLength);
    }
}
