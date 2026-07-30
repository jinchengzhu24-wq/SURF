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

    [Header("Initial LLM Loading")]
    public bool useInitialLLMLoadingTransition;
    public Text initialLLMLoadingText;
    public Button initialLLMRetryButton;
    public string initialLLMLoadingMessage = "LLM is generating...";
    public string initialLLMFailureMessage = "LLM generation failed.";
    public string initialLLMRetryLabel = "Retry";

    [Header("Complete Action")]
    public CompleteAction completeAction = CompleteAction.LoadNextScene;
    public string levelToLoad = "";
    public string menuSceneName = "Menu";

    [Header("Generated Level Limit")]
    public int maxGeneratedLevelCount;
    public GeneratedLevelLimitAction generatedLevelLimitAction = GeneratedLevelLimitAction.LoadNextScene;
    public GeneratedLevelLimitAction generatedLevelFailureAction = GeneratedLevelLimitAction.LoadNextScene;
    public int generatedLevelCount;

    [Header("Restart")]
    public bool allowRestartWithR = true;
    public KeyCode restartKey = KeyCode.R;

    private bool isCompletingLevel;
    private bool usesExternalInitialLoadingTransition;

    private void Start()
    {
        if (levelLoader == null)
        {
            levelLoader = FindObjectOfType<LevelLoader>();
        }

        StretchBlackPanelToFullscreen();
        EnsureInitialLLMRetryButton();

        if (usesExternalInitialLoadingTransition)
        {
            isCompletingLevel = true;
            SetPlayerInputEnabled(false);
            SetBlackPanelAlpha(0);
            return;
        }

        if (useInitialLLMLoadingTransition)
        {
            StartCoroutine(InitialLLMLoadingTransition());
            return;
        }

        ResetLevelState();
        StartCoroutine(Fade(1, 0));
    }

    public void BeginExternalInitialLoadingTransition()
    {
        usesExternalInitialLoadingTransition = true;
        isCompletingLevel = true;
        StretchBlackPanelToFullscreen();
        SetPlayerInputEnabled(false);
        SetBlackPanelAlpha(0);
    }

    public IEnumerator FadeToBlackForExternalInitialLoad()
    {
        if (!usesExternalInitialLoadingTransition)
        {
            BeginExternalInitialLoadingTransition();
        }

        SetPlayerInputEnabled(false);
        yield return Fade(GetBlackPanelAlpha(), 1);
    }

    public IEnumerator FadeFromBlackAfterExternalInitialLoad()
    {
        yield return Fade(GetBlackPanelAlpha(), 0);
        usesExternalInitialLoadingTransition = false;
        isCompletingLevel = false;
        ResetLevelState();
        SetPlayerInputEnabled(true);
    }

    private IEnumerator InitialLLMLoadingTransition()
    {
        isCompletingLevel = true;
        SetPlayerInputEnabled(false);
        SetBlackPanelAlpha(0);
        SetInitialLLMLoadingText(true, initialLLMLoadingMessage);
        SetInitialLLMRetryButtonVisible(false);

        if (levelLoader == null)
        {
            SetInitialLLMLoadingText(true, GetInitialLLMFailureMessage());
            SetInitialLLMRetryButtonVisible(true);
            yield break;
        }

        bool generatedLevel = false;
        yield return levelLoader.PrepareInitialLevelWithLLMPlanRoutine(result => generatedLevel = result);

        if (!generatedLevel)
        {
            SetBlackPanelAlpha(0);
            SetInitialLLMLoadingText(true, GetInitialLLMFailureMessage());
            SetInitialLLMRetryButtonVisible(true);
            yield break;
        }

        SetInitialLLMLoadingText(false, initialLLMLoadingMessage);
        SetInitialLLMRetryButtonVisible(false);
        yield return Fade(0, 1);

        if (!levelLoader.CommitPreparedInitialLevel())
        {
            SetBlackPanelAlpha(0);
            SetInitialLLMLoadingText(true, GetInitialLLMFailureMessage());
            SetInitialLLMRetryButtonVisible(true);
            yield break;
        }

        yield return Fade(1, 0);
        isCompletingLevel = false;
        SetPlayerInputEnabled(true);
    }

    private void RetryInitialLLMGeneration()
    {
        if (!useInitialLLMLoadingTransition)
        {
            return;
        }

        SetInitialLLMRetryButtonVisible(false);
        StartCoroutine(InitialLLMLoadingTransition());
    }

    private string GetInitialLLMFailureMessage()
    {
        return levelLoader != null && !string.IsNullOrEmpty(levelLoader.LastGenerationFailureMessage)
            ? levelLoader.LastGenerationFailureMessage
            : initialLLMFailureMessage;
    }

    private void EnsureInitialLLMRetryButton()
    {
        if (!useInitialLLMLoadingTransition)
        {
            return;
        }

        if (initialLLMRetryButton != null)
        {
            initialLLMRetryButton.onClick.RemoveListener(RetryInitialLLMGeneration);
            initialLLMRetryButton.onClick.AddListener(RetryInitialLLMGeneration);
            SetInitialLLMRetryButtonVisible(false);
            return;
        }

        if (initialLLMLoadingText == null)
        {
            return;
        }

        GameObject buttonObject = new GameObject(
            "InitialLLMRetryButton",
            typeof(RectTransform),
            typeof(Image),
            typeof(Button)
        );
        RectTransform buttonRect = buttonObject.GetComponent<RectTransform>();
        buttonRect.SetParent(initialLLMLoadingText.transform.parent, false);
        buttonRect.anchorMin = new Vector2(0.5f, 0.5f);
        buttonRect.anchorMax = new Vector2(0.5f, 0.5f);
        buttonRect.pivot = new Vector2(0.5f, 0.5f);
        buttonRect.anchoredPosition = initialLLMLoadingText.rectTransform.anchoredPosition
            + new Vector2(0f, -90f);
        buttonRect.sizeDelta = new Vector2(260f, 60f);

        Image buttonImage = buttonObject.GetComponent<Image>();
        buttonImage.color = new Color(0.1f, 0.35f, 0.75f, 1f);

        initialLLMRetryButton = buttonObject.GetComponent<Button>();
        initialLLMRetryButton.targetGraphic = buttonImage;
        initialLLMRetryButton.onClick.AddListener(RetryInitialLLMGeneration);

        GameObject labelObject = new GameObject(
            "Text",
            typeof(RectTransform),
            typeof(CanvasRenderer),
            typeof(Text)
        );
        RectTransform labelRect = labelObject.GetComponent<RectTransform>();
        labelRect.SetParent(buttonRect, false);
        labelRect.anchorMin = Vector2.zero;
        labelRect.anchorMax = Vector2.one;
        labelRect.offsetMin = Vector2.zero;
        labelRect.offsetMax = Vector2.zero;

        Text label = labelObject.GetComponent<Text>();
        label.text = initialLLMRetryLabel;
        label.font = initialLLMLoadingText.font;
        label.fontSize = Mathf.Max(24, initialLLMLoadingText.fontSize);
        label.fontStyle = FontStyle.Bold;
        label.alignment = TextAnchor.MiddleCenter;
        label.color = Color.white;
        label.raycastTarget = false;

        SetInitialLLMRetryButtonVisible(false);
    }

    private void SetInitialLLMRetryButtonVisible(bool visible)
    {
        if (initialLLMRetryButton != null)
        {
            initialLLMRetryButton.interactable = visible;
            initialLLMRetryButton.gameObject.SetActive(visible);
        }
    }

    private void SetInitialLLMLoadingText(bool visible, string message)
    {
        if (initialLLMLoadingText == null)
        {
            return;
        }

        initialLLMLoadingText.text = message;
        initialLLMLoadingText.gameObject.SetActive(visible);
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

        LevelStudyRecorder.RecordLevelRestarted();
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
        LevelStudyRecorder.RecordLevelCompleted();

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

            bool generatedNewLevel = false;
            yield return GenerateNewLevel(result => generatedNewLevel = result);

            if (!generatedNewLevel)
            {
                yield return HandleGeneratedLevelFailure();
                isCompletingLevel = false;
                yield break;
            }

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

        yield return HandleGeneratedLevelAction(generatedLevelLimitAction);
    }

    private IEnumerator HandleGeneratedLevelFailure()
    {
        Debug.LogWarning(
            "LevelManager generated level failed:"
            + " generatedLevelCount=" + generatedLevelCount
            + ", maxGeneratedLevelCount=" + maxGeneratedLevelCount
            + ", action=" + generatedLevelFailureAction
        );

        yield return HandleGeneratedLevelAction(generatedLevelFailureAction);
    }

    private IEnumerator HandleGeneratedLevelAction(GeneratedLevelLimitAction action)
    {
        if (action == GeneratedLevelLimitAction.LoadNextScene)
        {
            LoadNextScene();
        }
        else if (action == GeneratedLevelLimitAction.StopGame)
        {
            StopGame();
        }
        else if (action == GeneratedLevelLimitAction.LoadMenuScene)
        {
            LoadMenuScene();
        }
        else
        {
            yield return Fade(1, 0);
        }
    }

    private IEnumerator GenerateNewLevel(System.Action<bool> onComplete)
    {
        if (levelLoader == null)
        {
            levelLoader = FindObjectOfType<LevelLoader>();
        }

        if (levelLoader == null)
        {
            Debug.LogWarning("LevelManager: Cannot generate a new level because LevelLoader is missing.");
            onComplete?.Invoke(false);
            yield break;
        }

        if (levelLoader.useLLMPlan)
        {
            bool generatedLevel = false;
            yield return levelLoader.GenerateAndReloadWithLLMPlanRoutine(result => generatedLevel = result);
            onComplete?.Invoke(generatedLevel);
        }
        else
        {
            onComplete?.Invoke(levelLoader.GenerateAndReload());
        }
    }

    private void SetPlayerInputEnabled(bool enabled)
    {
        Player player = FindObjectOfType<Player>();

        if (player != null)
        {
            player.SetInputEnabled(enabled);
        }

        Player2 player2 = FindObjectOfType<Player2>();

        if (player2 != null)
        {
            player2.SetInputEnabled(enabled);
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

    private float GetBlackPanelAlpha()
    {
        return blackPanel != null ? blackPanel.color.a : 0;
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
        if (!string.IsNullOrEmpty(levelToLoad))
        {
            SceneManager.LoadScene(levelToLoad);
            return;
        }

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
