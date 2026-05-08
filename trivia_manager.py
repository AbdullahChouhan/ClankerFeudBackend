import csv
import urllib.parse
import random
import os

class TriviaManager:
    def __init__(self, data_directory="trivia_data"):
        self.trivia_database = []
        self._load_data(data_directory)

    def _load_data(self, data_directory):
        if not os.path.exists(data_directory):
            print(f"⚠️ Directory '{data_directory}' not found. Please create it and add your CSVs!")
            return

        files = [f for f in os.listdir(data_directory) if f.endswith('.csv')]
        
        for file in files:
            filepath = os.path.join(data_directory, file)
            category = file.replace('.csv', '')
            
            with open(filepath, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                for row in reader:
                    if len(row) >= 2:
                        # Decode the URL-encoded text
                        question = urllib.parse.unquote(row[0])
                        answer = urllib.parse.unquote(row[1])
                        
                        self.trivia_database.append({
                            "question": question, 
                            "answer": answer, 
                            "category": category
                        })
        
        print(f"📚 Loaded {len(self.trivia_database)} trivia questions into memory!")

    def get_random_question(self):
        if not self.trivia_database:
            return None
        return random.choice(self.trivia_database)