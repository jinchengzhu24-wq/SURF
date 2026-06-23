using System.Collections;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public class LevelManager : MonoBehaviour
{
    public enum CompleteAction
    {
        LoadNextScene,
        StayInCurrentScene,
        GenerateNewLevel
    }

    public enum GeneratedLevelLimitAction
    {
        LoadNextScene,
        StopGame,
        StayInCurrentScene
    }

    public PlayerAnimation anim;
    public LevelLoader levelLoader;

    [Header("Level Info")]
    public int boxCount;
    public int reachedCount;

    [Header("Black Panel")]
    public Image blackPanel;
    public float fadeTime = 1f;
    public float completeDelay = 1.5f;

    [Header("Complete Action")]
    public CompleteAction completeAction = CompleteAction.LoadNextScene;

    [Header("Generated Level Limit")]
    public int maxGeneratedLevelCount;
    public GeneratedLevelLimitAction generatedLevelLimitAction = GeneratedLevelLimitAction.LoadNextScene;
    public int generatedLevelCount;

    private void Start()
    {
        if (levelLoader == null)
        {
            levelLoader = FindObjectOfType<LevelLoader>();
        }

        ResetLevelState();
        StartCoroutine(Fade(1, 0));
    }

    public void BoxReachTarget()
    {
        reachedCount++;

        if (reachedCount == boxCount)
        {
            StartCoroutine(CompleteLevel());
        }
    }

    public void BoxLeaveTarget()
    {
        reachedCount--;
    }

    public void ResetLevelState()
    {
        boxCount = FindObjectsOfType<Box>().Length;
        reachedCount = 0;
    }

    public void RegisterGeneratedLevel()
    {
        generatedLevelCount++;

        Debug.Log(
            "LevelManager registered generated level:"
            + " generatedLevelCount=" + generatedLevelCount
            + ", maxGeneratedLevelCount=" + maxGeneratedLevelCount
        );
    }

    private IEnumerator CompleteLevel()
    {
        if (anim != null)
        {
            anim.Win();
        }

        yield return new WaitForSeconds(completeDelay);

        yield return Fade(0, 1);

        if (completeAction == CompleteAction.LoadNextScene)
        {
            LoadNextScene();
        }
        else if (completeAction == CompleteAction.GenerateNewLevel)
        {
            if (HasReachedGeneratedLevelLimit())
            {
                yield return HandleGeneratedLevelLimit();
                yield break;
            }

            yield return GenerateNewLevel();
            yield return Fade(1, 0);
        }
        else
        {
            yield return Fade(1, 0);
        }
    }

    private bool HasReachedGeneratedLevelLimit()
    {
        return maxGeneratedLevelCount > 0
            && generatedLevelCount >= maxGeneratedLevelCount;
    }

    private IEnumerator HandleGeneratedLevelLimit()
    {
        Debug.Log(
            "LevelManager generated level limit reached:"
            + " generatedLevelCount=" + generatedLevelCount
            + ", maxGeneratedLevelCount=" + maxGeneratedLevelCount
            + ", action=" + generatedLevelLimitAction
        );

        if (generatedLevelLimitAction == GeneratedLevelLimitAction.LoadNextScene)
        {
            LoadNextScene();
        }
        else if (generatedLevelLimitAction == GeneratedLevelLimitAction.StopGame)
        {
            StopGame();
        }
        else
        {
            yield return Fade(1, 0);
        }
    }

    private IEnumerator GenerateNewLevel()
    {
        if (levelLoader == null)
        {
            levelLoader = FindObjectOfType<LevelLoader>();
        }

        if (levelLoader == null)
        {
            Debug.LogWarning("LevelManager: Cannot generate a new level because LevelLoader is missing.");
            yield break;
        }

        if (levelLoader.useLLMPlan)
        {
            yield return levelLoader.GenerateAndReloadWithLLMPlanRoutine();
        }
        else
        {
            levelLoader.GenerateAndReload();
        }
    }

    private void LoadNextScene()
    {
        int currentIndex = SceneManager.GetActiveScene().buildIndex;
        int nextIndex = currentIndex + 1;

        if (nextIndex >= SceneManager.sceneCountInBuildSettings)
        {
            Debug.LogWarning("LevelManager: No next scene in Build Settings.");
            return;
        }

        SceneManager.LoadScene(nextIndex);
    }

    private void StopGame()
    {
        Debug.Log("LevelManager: Stop game requested.");

#if UNITY_EDITOR
        UnityEditor.EditorApplication.isPlaying = false;
#else
        Application.Quit();
#endif
    }

    private IEnumerator Fade(float startAlpha, float endAlpha)
    {
        if (blackPanel == null)
        {
            yield break;
        }

        float timer = 0;

        while (timer < fadeTime)
        {
            timer += Time.deltaTime;

            Color color = blackPanel.color;
            color.a = Mathf.Lerp(startAlpha, endAlpha, timer / fadeTime);
            blackPanel.color = color;

            yield return null;
        }

        Color finalColor = blackPanel.color;
        finalColor.a = endAlpha;
        blackPanel.color = finalColor;
    }
}
