import os
from sqlalchemy import create_engine, text
from pymongo import MongoClient
from google import genai
from dotenv import load_dotenv

load_dotenv(override=True)

# Set your Gemini API Key here or in environment variables
# For now, we will assume it's in the environment variable GEMINI_API_KEY
# If not set, it will mock the LLM response for demonstration purposes

class ChatManager:
    def __init__(self):
        self.state = 'AWAITING_DB_TYPE'
        self.db_type = None
        self.hosting_type = None
        self.uri = None
        self.engine = None
        self.mongo_client = None
        self.mongo_db = None
        
        self.api_key = os.environ.get("GEMINI_API_KEY")
        if self.api_key:
            self.client = genai.Client(api_key=self.api_key)
        else:
            self.client = None

    def process_message(self, message: str) -> dict:
        if self.state == 'AWAITING_DB_TYPE':
            return self._handle_db_type(message)
        elif self.state == 'AWAITING_HOSTING_TYPE':
            return self._handle_hosting_type(message)
        elif self.state == 'AWAITING_URI':
            return self._handle_uri(message)
        elif self.state == 'CONNECTED':
            return self._handle_query(message)
        else:
            return {"reply": "Unknown state. Let's restart. What database type?", "options": ['PostgreSQL', 'MySQL', 'MongoDB'], "state": 'AWAITING_DB_TYPE'}

    def _handle_db_type(self, message: str) -> dict:
        db_type = message.lower().strip()
        if 'postgres' in db_type:
            self.db_type = 'PostgreSQL'
        elif 'mysql' in db_type or 'sql' in db_type:
            self.db_type = 'MySQL'
        elif 'mongo' in db_type:
            self.db_type = 'MongoDB'
        else:
            return {
                "reply": "I didn't recognize that database type. Please choose from PostgreSQL, MySQL, or MongoDB.",
                "options": ['PostgreSQL', 'MySQL', 'MongoDB'],
                "state": self.state
            }
        
        self.state = 'AWAITING_HOSTING_TYPE'
        return {
            "reply": f"Great! You selected {self.db_type}. Is this database hosted locally or in the cloud?",
            "options": ['Local', 'Cloud'],
            "state": self.state
        }

    def _handle_hosting_type(self, message: str) -> dict:
        hosting = message.lower().strip()
        if 'local' in hosting:
            self.hosting_type = 'Local'
        elif 'cloud' in hosting:
            self.hosting_type = 'Cloud'
        else:
            return {
                "reply": "Please select either Local or Cloud.",
                "options": ['Local', 'Cloud'],
                "state": self.state
            }
            
        self.state = 'AWAITING_URI'
        example_uri = ""
        if self.db_type == 'PostgreSQL':
            example_uri = "postgresql://user:password@localhost:5432/dbname" if self.hosting_type == 'Local' else "postgresql://user:password@cloud-host:5432/dbname"
        elif self.db_type == 'MySQL':
            example_uri = "mysql+pymysql://user:password@localhost:3306/dbname" if self.hosting_type == 'Local' else "mysql+pymysql://user:password@cloud-host:3306/dbname"
        elif self.db_type == 'MongoDB':
            example_uri = "mongodb://localhost:27017/" if self.hosting_type == 'Local' else "mongodb+srv://user:password@cluster.mongodb.net/"
            
        return {
            "reply": f"Understood. Please provide the connection URI/URL for your {self.hosting_type} {self.db_type} database.\nExample: `{example_uri}`",
            "state": self.state
        }

    def _handle_uri(self, message: str) -> dict:
        self.uri = message.strip()
        
        # Automatically fix mysql:// to mysql+pymysql://
        if self.db_type == 'MySQL' and self.uri.startswith('mysql://'):
            self.uri = self.uri.replace('mysql://', 'mysql+pymysql://', 1)
            
        try:
            if self.db_type in ['PostgreSQL', 'MySQL']:
                self.engine = create_engine(self.uri)
                # Test connection
                with self.engine.connect() as conn:
                    pass
            elif self.db_type == 'MongoDB':
                self.mongo_client = MongoClient(self.uri, serverSelectionTimeoutMS=5000)
                # Test connection
                self.mongo_client.server_info()
                # Try to get default db
                db_name = self.uri.split('/')[-1].split('?')[0]
                if not db_name:
                    db_name = 'test'
                self.mongo_db = self.mongo_client[db_name]
                
            self.state = 'CONNECTED'
            return {
                "reply": f"Successfully connected to the {self.db_type} database! What would you like to query?",
                "state": self.state,
                "db_type": self.db_type
            }
        except Exception as e:
            return {
                "reply": f"Failed to connect. Error: {str(e)}\nPlease check your URI and try again.",
                "state": self.state
            }

    def _generate_sql_query(self, natural_query: str) -> str:
        if not self.client:
            raise ValueError("No API Key found! Please set GEMINI_API_KEY environment variable and restart the server.")
            
        prompt = f"""
        Translate the following natural language query into a valid SQL query for a {self.db_type} database.
        Return ONLY the raw SQL query, without markdown blocks, without explanations.
        Query: {natural_query}
        """
        response = self.client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        query = response.text.strip()
        if query.startswith('```sql'):
            query = query[6:]
        if query.startswith('```'):
            query = query[3:]
        if query.endswith('```'):
            query = query[:-3]
            
        return query.strip()

    def _generate_mongo_query(self, natural_query: str) -> dict:
        if not self.client:
            raise ValueError("No API Key found! Please set GEMINI_API_KEY environment variable and restart the server.")
            
        prompt = f"""
        Translate the following natural language query into a MongoDB query.
        Return a JSON object with two keys:
        - "collection": the name of the collection
        - "filter": the JSON filter object for the find() method.
        Do not wrap in markdown blocks, return ONLY valid JSON.
        Query: {natural_query}
        """
        response = self.client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        import json
        text_resp = response.text.strip()
        if text_resp.startswith('```json'):
            text_resp = text_resp[7:]
        if text_resp.startswith('```'):
            text_resp = text_resp[3:]
        if text_resp.endswith('```'):
            text_resp = text_resp[:-3]
        
        try:
            return json.loads(text_resp.strip())
        except:
            return {"collection": "unknown", "query": {}}

    def _handle_query(self, message: str) -> dict:
        try:
            if self.db_type in ['PostgreSQL', 'MySQL']:
                sql_query = self._generate_sql_query(message)
                
                # Execute query
                # Note: In a production app, we MUST validate and sanitize this query to prevent destructive operations.
                with self.engine.connect() as conn:
                    result = conn.execute(text(sql_query))
                    if not result.returns_rows:
                        conn.commit()
                        return {
                            "reply": "Query executed successfully.",
                            "query": sql_query,
                            "state": self.state,
                            "db_type": self.db_type
                        }
                    else:
                        columns = result.keys()
                        rows = result.fetchmany(10)
                        data = [dict(zip(columns, row)) for row in rows]
                        return {
                            "reply": "Here are the results for your query:",
                            "query": sql_query,
                            "data": data,
                            "state": self.state,
                            "db_type": self.db_type
                        }
                        
            elif self.db_type == 'MongoDB':
                mongo_q = self._generate_mongo_query(message)
                collection_name = mongo_q.get("collection")
                filter_q = mongo_q.get("filter", {})
                
                if not collection_name:
                    return {"reply": "Could not determine collection name.", "state": self.state, "db_type": self.db_type}
                    
                collection = self.mongo_db[collection_name]
                # Execute the find query and return results
                cursor = collection.find(filter_q).limit(10)
                data = []
                for doc in cursor:
                    doc['_id'] = str(doc['_id'])
                    data.append(doc)
                    
                if len(data) > 0:
                    return {
                        "reply": f"Found {len(data)} results in collection '{collection_name}':",
                        "query": f"db.{collection_name}.find({filter_q})",
                        "data": data,
                        "state": self.state,
                        "db_type": self.db_type
                    }
                else:
                    return {
                        "reply": f"Query executed successfully on collection '{collection_name}', but returned no data.",
                        "query": f"db.{collection_name}.find({filter_q})",
                        "state": self.state,
                        "db_type": self.db_type
                    }
                
        except Exception as e:
            return {
                "reply": f"Error executing query: {str(e)}",
                "state": self.state,
                "db_type": self.db_type
            }
