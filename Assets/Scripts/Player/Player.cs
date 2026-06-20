using System.Collections;
using System.Collections.Generic;
using UnityEngine;

public class Player : MonoBehaviour
{
    public float moveDistance = 1f;

    private void Update()
    {
        Move();
    }

    private void Move()
    {
        Vector3 dir = Vector3.zero;

        if (Input.GetKeyDown(KeyCode.W))
        {
            dir = Vector3.up;
        }

        if (Input.GetKeyDown(KeyCode.S))
        {
            dir = Vector3.down;
        }

        if (Input.GetKeyDown(KeyCode.A))
        {
            dir = Vector3.left;
        }

        if (Input.GetKeyDown(KeyCode.D))
        {
            dir = Vector3.right;
        }

        if (dir == Vector3.zero)
        {
            return;
        }

        Vector3 nextPos = transform.position + dir * moveDistance;

        if (IsBlocked(nextPos))
        {
            return;
        }

        Box box = GetBox(nextPos);

        if (box != null)
        {
            Vector3 boxNextPos = box.transform.position + dir * moveDistance;

            if (IsBlocked(boxNextPos) || GetBox(boxNextPos) != null)
            {
                return;
            }

            box.Move(dir * moveDistance);
        }

        transform.position = nextPos;
    }

    private bool IsBlocked(Vector3 position)
    {
        Collider2D hit = Physics2D.OverlapPoint(position);

        if (hit == null)
        {
            return false;
        }

        return hit.CompareTag("Wall") || hit.CompareTag("Water");
    }

    private Box GetBox(Vector3 position)
    {
        Collider2D hit = Physics2D.OverlapPoint(position);

        if (hit == null)
        {
            return null;
        }

        return hit.GetComponent<Box>();
    }
}
