using System;
using System.Collections;
using System.Collections.Generic;
using UnityEngine;

public partial class LevelGenerator
{
    private bool TryGenerateWithCurrentRules(string mode, int maxAttempts, bool logRejections)
    {
        float modeStartedAt = Time.realtimeSinceStartup;
        int rejectedBySolve = 0;
        int rejectedByDifficulty = 0;
        int rejectedByParse = 0;
        int rejectedByRepeat = 0;
        int rejectedByQuality = 0;
        int rejectedByObstacleInfluence = 0;
        int rejectedByCorridor = 0;
        int scoredCandidates = 0;
        int selectableCandidates = 0;
        int qualifiedCandidates = 0;
        int candidatesSinceFirstQualified = 0;
        int[] candidateFailures = new int[(int)CandidateFailureReason.BuildRows + 1];
        int candidateSampleTarget = GetCandidateSampleTarget();
        GeneratedCandidate bestCandidate = null;
        GeneratedCandidate bestQualifiedCandidate = null;
        List<GeneratedCandidate> topLLMQualifiedCandidates = new List<GeneratedCandidate>();

        for (int attempt = 1; attempt <= maxAttempts; attempt++)
        {
            if (activeLLMQualityGate && hasDesignBlueprint)
            {
                currentStructureTemplate =
                    LevelGenerationTemplates.GetStructureTemplate(currentArchetype, random);
            }

            if (!TryCreateCandidate(out string[] rows, out int reversePulls, out CandidateFailureReason failureReason))
            {
                candidateFailures[(int)failureReason]++;
                continue;
            }

            if (IsRecentlyGeneratedLayout(rows, true))
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

            CorridorValidationResult corridorValidation = ValidateCorridorStructure(rows);
            bool requiresBoxPassage = currentCorridorRole == "required_box_route";
            bool solved;
            int searchedStates;
            int solutionSteps;
            int pushCount;
            bool boxPassedThrough = false;

            if (requiresBoxPassage && candidateCorridorOpeningCells.Count > 0)
            {
                solved = levelSolver.CanSolveWithRequiredBoxCells(
                    new HashSet<Vector2Int>(candidateCorridorOpeningCells),
                    out searchedStates,
                    out solutionSteps,
                    out pushCount,
                    out boxPassedThrough
                );
            }
            else
            {
                solved = levelSolver.CanSolve(out searchedStates, out solutionSteps, out pushCount);
            }

            corridorValidation.playerCanPass = solved && corridorValidation.uniquePassage;
            corridorValidation.boxPassedThrough = boxPassedThrough;
            corridorValidation.verified = corridorValidation.uniquePassage
                && corridorValidation.playerCanPass
                && (!requiresBoxPassage || boxPassedThrough);
            corridorValidation.message = corridorValidation.verified
                ? "Requested corridor was generated and verified."
                : GetCorridorFailureMessage(corridorValidation, requiresBoxPassage);

            if (!solved)
            {
                rejectedBySolve++;
                continue;
            }

            if (HasRequiredCorridor() && !corridorValidation.verified)
            {
                rejectedByCorridor++;
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

            GeneratedCandidate candidate = new GeneratedCandidate(
                CloneRows(rows),
                attempt,
                quality,
                corridorValidation.Copy()
            );
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

                if (RequiresLLMQualityGate())
                {
                    AddToTopCandidatePool(
                        topLLMQualifiedCandidates,
                        candidate,
                        LLMTopCandidatePoolSize
                    );
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
        string selectionMode = "best";
        int selectionPoolSize = selectedCandidate == null ? 0 : 1;
        int selectedRank = selectedCandidate == null ? 0 : 1;

        if (RequiresLLMQualityGate() && topLLMQualifiedCandidates.Count > 0)
        {
            selectionPoolSize = topLLMQualifiedCandidates.Count;
            int selectedIndex = random.Next(selectionPoolSize);
            selectedCandidate = topLLMQualifiedCandidates[selectedIndex];
            selectedRank = selectedIndex + 1;
            selectionMode = "llm-top3-random";
        }

        if (selectedCandidate == null && !RequiresLLMQualityGate())
        {
            selectedCandidate = bestCandidate;
            selectionPoolSize = selectedCandidate == null ? 0 : 1;
            selectedRank = selectedCandidate == null ? 0 : 1;
        }

        if (selectedCandidate != null)
        {
            AcceptCandidate(
                selectedCandidate,
                mode,
                scoredCandidates,
                selectableCandidates,
                qualifiedCandidates,
                GetElapsedMilliseconds(modeStartedAt),
                selectionMode,
                selectionPoolSize,
                selectedRank
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
                + ", rejectedByCorridor=" + rejectedByCorridor
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
            return qualifiedCandidates >= LLMTopCandidatePoolSize
                && candidatesSinceFirstQualified > candidateSampleTarget;
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

    private void AddToTopCandidatePool(
        List<GeneratedCandidate> topCandidates,
        GeneratedCandidate candidate,
        int maxCount)
    {
        int insertIndex = topCandidates.Count;

        for (int i = 0; i < topCandidates.Count; i++)
        {
            if (IsBetterCandidate(candidate, topCandidates[i]))
            {
                insertIndex = i;
                break;
            }
        }

        topCandidates.Insert(insertIndex, candidate);

        if (topCandidates.Count > maxCount)
        {
            topCandidates.RemoveAt(topCandidates.Count - 1);
        }
    }

    private bool MeetsRequiredQuality(LevelQualityReport quality)
    {
        if (RequiresLLMQualityGate())
        {
            int minimumWaterTiles = activeRelaxedBlueprint
                ? rules.llmRelaxedMinimumWaterTiles
                : rules.llmMinimumWaterTiles;
            int minimumSurroundedWalls = activeRelaxedBlueprint
                ? rules.llmRelaxedMinimumSurroundedWalls
                : rules.llmMinimumSurroundedWalls;
            int minimumBoxInteractionScore = activeRelaxedBlueprint
                ? rules.llmRelaxedMinimumBoxInteractionScore
                : rules.llmMinimumBoxInteractionScore;
            int minimumQualityScore = activeRelaxedBlueprint
                ? rules.llmRelaxedMinimumQualityScore
                : rules.llmMinimumQualityScore;

            if (rules.maxWaterAreas == 0)
            {
                minimumWaterTiles = 0;
            }

            if (rules.maxWallObstacleBlocks == 0 && currentCorridorPlacement == "none")
            {
                minimumSurroundedWalls = 0;
            }

            return quality.waterTiles >= minimumWaterTiles
                && quality.surroundedWallCount >= minimumSurroundedWalls
                && quality.boxInteractionScore >= minimumBoxInteractionScore
                && quality.structureSimilarity < GetEffectiveStructureSimilarityThreshold()
                && quality.score >= minimumQualityScore;
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
        int elapsedMs,
        string selectionMode,
        int selectionPoolSize,
        int selectedRank)
    {
        levelData.rows = CloneRows(candidate.rows);
        levelSolver.levelData = levelData;
        lastAttempts = candidate.attempt;
        lastReversePulls = candidate.quality.reversePulls;
        lastSearchedStates = candidate.quality.searchedStates;
        lastSolutionSteps = candidate.quality.solutionSteps;
        lastPushes = candidate.quality.pushes;
        LastCorridorValidation = candidate.corridorValidation == null
            ? null
            : candidate.corridorValidation.Copy();
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
                + ", selectionMode=" + selectionMode
                + ", selectionPoolSize=" + selectionPoolSize
                + ", selectedRank=" + selectedRank
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
        quality.boxInteractionScore = GetBoxInteractionScore(rows);
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
            || (rejectSimilar && structureSimilarity >= GetEffectiveStructureSimilarityThreshold());

        if (repeated && logGenerationResult)
        {
            Debug.Log(
                "LevelGenerator rejected repeated layout:"
                + " recentGeneratedLayoutHistory=" + recentGeneratedLayoutHistory
                + ", structureSimilarity=" + structureSimilarity
                + ", threshold=" + GetEffectiveStructureSimilarityThreshold()
            );
        }

        return repeated;
    }

    private int GetEffectiveStructureSimilarityThreshold()
    {
        return activeRelaxedBlueprint
            ? rules.llmRelaxedStructureSimilarityThreshold
            : rules.recentStructureSimilarityThreshold;
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
