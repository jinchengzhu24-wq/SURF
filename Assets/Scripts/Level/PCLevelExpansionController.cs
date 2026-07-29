using System.Collections;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public class PCLevelExpansionController : MonoBehaviour
{
    private const string GenerationFailureMessage =
        "Generation failed. Retry or adjust the design.";

    [Header("Generation")]
    [SerializeField] private PCLevelExpansionClient expansionClient;
    [SerializeField] private LevelData levelData;
    [SerializeField] private LevelLoader levelLoader;
    [SerializeField] private LevelManager levelManager;
    [SerializeField] private LevelSolver levelSolver;
    [SerializeField] private int maximumModelAttempts = 2;

    [Header("Candidate Rules")]
    [SerializeField] private int minimumActivityArea = 48;
    [SerializeField] private int minimumWaterWidth = 2;
    [SerializeField] private int minimumWaterHeight = 2;
    [SerializeField] private int maximumWaterWidth = 4;
    [SerializeField] private int maximumWaterHeight = 4;

    [Header("Static UI")]
    [SerializeField] private Text statusText;
    [SerializeField] private Button retryButton;
    [SerializeField] private Button backButton;
    [SerializeField] private string designSceneName = "PC_Design";

    private PCDesignSketchData sketch;
    private Coroutine generationRoutine;

    private void Awake()
    {
        SetButtonVisible(retryButton, false);
        SetButtonVisible(backButton, false);
        ValidateSceneReferences();
    }

    private void Start()
    {
        if (!PCDesignContext.TryLoad(out sketch))
        {
            ShowFailure("No saved PC design was found.");
            return;
        }

        StartGenerationBatch();
    }

    public void RetryGeneration()
    {
        if (generationRoutine == null)
        {
            StartGenerationBatch();
        }
    }

    public void BackToDesign()
    {
        if (generationRoutine != null)
        {
            StopCoroutine(generationRoutine);
            generationRoutine = null;
        }

        PCDesignContext.RequestDesignRestore();

        if (string.IsNullOrWhiteSpace(designSceneName)
            || !Application.CanStreamedLevelBeLoaded(designSceneName))
        {
            ShowFailure("PC_Design is not available in Build Settings.");
            return;
        }

        SceneManager.LoadScene(designSceneName);
    }

    private void StartGenerationBatch()
    {
        SetButtonVisible(retryButton, false);
        SetButtonVisible(backButton, false);
        generationRoutine = StartCoroutine(GenerateLevelRoutine());
    }

    private IEnumerator GenerateLevelRoutine()
    {
        int remainingAttempts = Mathf.Clamp(maximumModelAttempts, 1, 2);
        string[] previousCandidateRows = null;
        string rejectionReason = "";
        string failureMessage = "PC level generation failed.";

        while (remainingAttempts > 0)
        {
            int attemptNumber = maximumModelAttempts - remainingAttempts + 1;
            ShowStatus(
                "LLM is expanding the PC design...\nAttempt "
                    + attemptNumber
                    + " of "
                    + maximumModelAttempts
            );

            PCLevelCandidateResponse response = null;
            yield return expansionClient.RequestCandidate(
                sketch,
                previousCandidateRows,
                rejectionReason,
                remainingAttempts,
                result => response = result
            );

            int attemptsUsed = Mathf.Clamp(
                expansionClient.LastAttemptsUsed,
                1,
                remainingAttempts
            );
            remainingAttempts = Mathf.Max(0, remainingAttempts - attemptsUsed);

            if (response == null)
            {
                failureMessage = GenerationFailureMessage;
                break;
            }

            previousCandidateRows = CloneRows(response.rows);

            if (!PCLevelCandidateValidator.TryValidate(
                    sketch,
                    response.rows,
                    minimumActivityArea,
                    minimumWaterWidth,
                    minimumWaterHeight,
                    maximumWaterWidth,
                    maximumWaterHeight,
                    out rejectionReason))
            {
                failureMessage = GenerationFailureMessage;
                Debug.LogWarning(
                    "PCLevelExpansionController rejected candidate: "
                    + rejectionReason
                );
                continue;
            }

            levelData.rows = CloneRows(response.rows);
            levelSolver.levelData = levelData;
            int searchedStates = 0;
            int solutionSteps = -1;
            int pushCount = -1;
            bool parsedLevel = levelSolver.ParseLevel();
            bool solvedLevel = parsedLevel
                && levelSolver.CanSolve(
                    out searchedStates,
                    out solutionSteps,
                    out pushCount
                );

            if (!solvedLevel)
            {
                rejectionReason = "Unity LevelSolver rejected the candidate as unsolvable.";
                failureMessage = GenerationFailureMessage;
                Debug.LogWarning(
                    "PCLevelExpansionController rejected candidate:"
                    + " searchedStates="
                    + searchedStates
                    + ", solutionSteps="
                    + solutionSteps
                    + ", pushCount="
                    + pushCount
                );
                continue;
            }

            levelLoader.levelData = levelData;
            levelLoader.LoadLevel();

            if (levelManager != null)
            {
                levelManager.RegisterGeneratedLevel();
            }

            HideGenerationUi();
            generationRoutine = null;
            yield break;
        }

        generationRoutine = null;
        ShowFailure(failureMessage);
    }

    private void ShowStatus(string message)
    {
        if (statusText != null)
        {
            statusText.color = Color.white;
            statusText.text = message;
            statusText.gameObject.SetActive(true);
        }
    }

    private void ShowFailure(string message)
    {
        if (statusText != null)
        {
            statusText.color = new Color(1f, 0.3f, 0.3f, 1f);
            statusText.text = message;
            statusText.gameObject.SetActive(true);
        }

        SetButtonVisible(retryButton, true);
        SetButtonVisible(backButton, true);
    }

    private void HideGenerationUi()
    {
        if (statusText != null)
        {
            statusText.gameObject.SetActive(false);
        }

        SetButtonVisible(retryButton, false);
        SetButtonVisible(backButton, false);
    }

    private static void SetButtonVisible(Button button, bool visible)
    {
        if (button == null)
        {
            return;
        }

        button.interactable = visible;
        button.gameObject.SetActive(visible);
    }

    private static string[] CloneRows(string[] rows)
    {
        if (rows == null)
        {
            return null;
        }

        string[] clone = new string[rows.Length];

        for (int row = 0; row < rows.Length; row++)
        {
            clone[row] = rows[row] ?? "";
        }

        return clone;
    }

    private void ValidateSceneReferences()
    {
        if (expansionClient == null
            || levelData == null
            || levelLoader == null
            || levelSolver == null
            || statusText == null
            || retryButton == null
            || backButton == null)
        {
            Debug.LogError(
                "PCLevelExpansionController: A serialized scene reference is missing.",
                this
            );
        }
    }
}
