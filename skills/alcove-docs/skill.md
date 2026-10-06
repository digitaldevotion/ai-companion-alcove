---
name: alcoveDocs
description: Enables quick read-only access to Alcove's official online documentation, instructions for downloading Google Doc content, etc. Use when a user asks about Alcove documentation/manuals, Alcove !commands, Alcove behaviors, etc. needs help finding docs, or wants to read/download a specific Alcove guide.
---

## Documentation Site Links

The following URLs are currently available on the documentation site. You will HAVE to reformat them to read them programatically (See next section):

* Upgrade Manual & New Features - https://docs.google.com/document/d/1Ol7vSUaYFqL8fnyqMvmSutRRc4cs3X2WxcKotI8QRk0/edit?usp=sharing
* macOS Setup - https://docs.google.com/document/d/1PhXT7GX4fGJQ3hylHXh5l5hBrhBTk6mvyN4KF1KSDBU/edit?usp=drive_link
* Windows Setup - https://docs.google.com/document/d/1nPWmsMzuBe0J9o3uCBx1k8ngluOUA0vh_UufVRtQApo/edit?usp=sharing
* Linux Setup - https://docs.google.com/document/d/1Ao65X5zvo6_70vjuN23NOPucwdrGS_f5BMmrmCz2jiA/edit?usp=drive_link
* Online Services Setup - https://docs.google.com/document/d/1KK1jpk_tfEH7cLYA2-d0k7Jtxlw-iOcybZWEDvfSZuY/edit?usp=sharing
* Alcove Engine Setup - https://docs.google.com/document/d/1UMiHjm3PHkOviO6LNjTDvzEnn1U8c3qNkODlCA1E2JA/edit?usp=sharing
* User's Guide - https://docs.google.com/document/d/1ehPRcFKFQjRT3vjbKpAnup2vRE6KUxhsussH7wEhOOs/edit?usp=drive_link


## Reading Google Docs Linked from the Documentation Site

The Google Docs on the documentation site are shared as "Anyone with the link" but require JavaScript to render in a browser. To read their content programmatically, use Google's **export endpoint**:

### Export URL Format
```
https://docs.google.com/document/d/{DOCUMENT_ID}/export?format=txt
```

### How to Find the Document ID
From any Google Docs share link in the format:
```
https://docs.google.com/document/d/1qnjuomZicKJFnnURYWTsnNm-lEeNZgH-4qVn_6FrZ-M/edit?usp=sharing
```
The document ID is the long string between `/d/` and `/edit`:
```
1qnjuomZicKJFnnURYWTsnNm-lEeNZgH-4qVn_6FrZ-M
```

### Methods to Fetch Content

**Using the `runcmd` tool to read the documentation:**

```
<runCmd>
curl -s -L https://docs.google.com/document/d/{DOCUMENT_ID}/export?format=txt
</runCmd>
```

⚠️ Note: Using the regular `/edit?usp=sharing` URL with `readweb` will return a "JavaScript not enabled" page. Always use the `/export?format=txt` endpoint instead.

## Source Code

The Alcove source code can also be examined (for read-only purposes only) if questions come up about specific variables in the config.py or config_template.py files that can't be found in the user manual. The source code URL is:

**https://github.com/digitaldevotion/ai-companion-alcove**