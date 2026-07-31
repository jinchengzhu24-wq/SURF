using UnityEngine;
using UnityEngine.SceneManagement;

public class OnlineChallengeCompletionRoute : MonoBehaviour
{
    private const string WaitingSceneName = "Challenge_Waiting";

    private LevelManager levelManager;
    private LevelData levelData;

    private void Start()
    {
        levelManager = FindObjectOfType<LevelManager>();
        levelData = FindObjectOfType<LevelData>();

        if (levelManager != null)
        {
            levelManager.CompletionTransitionRequested += HandleLevelCompleted;
        }
    }

    private void OnDestroy()
    {
        if (levelManager != null)
        {
            levelManager.CompletionTransitionRequested -= HandleLevelCompleted;
        }
    }

    private void HandleLevelCompleted(LevelManager manager)
    {
        if (!OnlineMatchContext.HasMatch
            || levelData == null
            || levelData.rows == null)
        {
            return;
        }

        if (!Application.CanStreamedLevelBeLoaded(WaitingSceneName))
        {
            Debug.LogError(
                "Online challenge waiting scene is not available in Build Settings."
            );
            return;
        }

        OnlineMatchContext.StageChallenge(
            levelData.rows,
            CompetitionModeController.GetSelectedMode(),
            AIAssistantModeController.GetSelectedApiMode()
        );
        manager.MarkCompletionTransitionHandled();
        SceneManager.LoadScene(WaitingSceneName);
    }
}
