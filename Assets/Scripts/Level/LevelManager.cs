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
        GenerateNewLevel,
        LoadMenuScene
    }

    public enum GeneratedLevelLimitAction
    {
        LoadNextScene,
        StopGame,
        StayInCurrentScene,
        LoadMenuScene
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
    public string menuSceneName = "Menu";

    [Header("Generated Level Limit")]
    public int maxGeneratedLevelCount;
    public GeneratedLevelLimitAction generatedLevelLimitAction = GeneratedLevelLimitAction.LoadNextScene;
    public int generatedLevelCount;

    [Header("Restart")]
    public bool allowRestartWithR = true;
    public KeyCode restartKey = KeyCode.R;

    private bool isCompletingLevel;

    private void Start()
    {
        if (levelLoader == null)
        {
            levelLoader = FindObjectOfType<LevelLoader>();
        }

        StretchBlackPanelToFullscreen();
        ResetLevelState();
        StartCoroutine(Fade(1, 0));
    }

    private void Update()
    {
        if (!allowRestartWithR || isCompletingLevel)
        {
            return;
        }

        if (Input.GetKeyDown(restartKey))
        {
            RestartCurrentLevel();
        }
    }

    public void BoxReachTarget()
    {
        if (isCompletingLevel)
        {
            return;
        }

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

        if (!isCompletingLevel)
        {
            SetPlayerInputEnabled(true);
        }
    }

    public void RestartCurrentLevel()
    {
        if (levelLoader == null)
        {
            levelLoader = FindObjectOfType<LevelLoader>();
        }

        if (levelLoader == null)
        {
            Debug.LogWarning("LevelManager: Cannot restart because LevelLoader is missing.");
            return;
        }

        Debug.Log("LevelManager restarted current level.");

        levelLoader.LoadLevel();
        SetPlayerInputEnabled(true);
        SetBlackPanelAlpha(0);
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
        isCompletingLevel = true;
        SetPlayerInputEnabled(false);

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
                isCompletingLevel = false;
                yield break;
            }

            yield return GenerateNewLevel();
            yield return Fade(1, 0);
            SetPlayerInputEnabled(true);
        }
        else if (completeAction == CompleteAction.LoadMenuScene)
        {
            LoadMenuScene();
        }
        else
        {
            yield return Fade(1, 0);
        }

        isCompletingLevel = false;
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
        else if (generatedLevelLimitAction == GeneratedLevelLimitAction.LoadMenuScene)
        {
            LoadMenuScene();
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

    private void SetPlayerInputEnabled(bool enabled)
    {
        Player player = FindObjectOfType<Player>();

        if (player != null)
        {
            player.SetInputEnabled(enabled);
        }
    }

    private void SetBlackPanelAlpha(float alpha)
    {
        if (blackPanel == null)
        {
            return;
        }

        Color color = blackPanel.color;
        color.a = alpha;
        blackPanel.color = color;
    }

    private void StretchBlackPanelToFullscreen()
    {
        if (blackPanel == null)
        {
            return;
        }

        RectTransform rectTransform = blackPanel.transform as RectTransform;

        if (rectTransform == null)
        {
            return;
        }

        rectTransform.anchorMin = Vector2.zero;
        rectTransform.anchorMax = Vector2.one;
        rectTransform.offsetMin = Vector2.zero;
        rectTransform.offsetMax = Vector2.zero;
        rectTransform.localScale = Vector3.one;
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

    private void LoadMenuScene()
    {
        if (string.IsNullOrEmpty(menuSceneName))
        {
            Debug.LogWarning("LevelManager: Menu scene name is empty.");
            return;
        }

        SceneManager.LoadScene(menuSceneName);
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
