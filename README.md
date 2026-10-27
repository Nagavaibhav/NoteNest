# NoteNest - Student Notes Organizer

NoteNest is a local Flask and SQLite application for keeping college notes organized. It does not use an AI API or send note text to an external service.

## Features completed

### DA-1

- Student registration and sign-in
- PDF, PPT/PPTX, DOCX and TXT uploads
- Local text extraction and keyword-based subject classification
- Note storage, basic search, download and deletion

### DA-2

- Relevance-ranked search using title, subject, topic and text matches
- Automatic topic labels from subject rules and frequent terms in the uploaded text
- Advanced filtering by subject, module, file type and bookmark status
- Bookmarks for important notes
- Module-wise organization on upload, with editing from each note page
- ZIP export and import that retains files, extracted text, topics, modules and bookmarks

## Run

```bash
python3 -m pip install -r requirements.txt
python3 app.py
```

